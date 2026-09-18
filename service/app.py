"""应用服务层：接收命令 -> 调引擎核算 -> 追加事件 -> 注册幂等回执。

所有写操作要求带 Idempotency-Key（渠道重试/离线补录复用同一把键），
重复请求返回首次结果，不产生第二条事件。
"""
from __future__ import annotations

import functools
import math
import threading
from collections import defaultdict
from datetime import datetime, timezone

from . import domain as dm
from . import engine
from .store import EventStore, replay


class ServiceError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = str(message)
        self.details = details or {}


def _transactional(fn):
    """写命令事务：成功则整批事件一次落盘；任何异常都回滚到上次提交点。"""
    @functools.wraps(fn)
    def wrapper(self, key, *args, **kwargs):
        with self.lock:
            self._pending = []
            try:
                result = fn(self, key, *args, **kwargs)
            except Exception:
                self.state = replay(self.store.all())
                self._pending = []
                raise
            if self._pending:
                raise ServiceError("TXN_NOT_COMMITTED",
                                   f"{fn.__name__} 产生了事件但未提交")
            return result
    return wrapper


class CapacityService:
    def __init__(self, store: EventStore, clock=None):
        self.store = store
        self.lock = threading.RLock()
        self.state = replay(store.all())
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self._pending: list[dict] = []

    # ---- 内部 ----

    def _now(self) -> str:
        return self.clock()

    def _new_id(self, prefix: str) -> str:
        n = self.state.counters.get(prefix, 0) + 1
        self.state.counters[prefix] = n
        return f"{prefix}-{n:04d}"

    def _append(self, etype: str, data: dict) -> dict:
        """暂存事件并立即折叠到内存（命令内后续逻辑可见），成功时随事务统一提交。"""
        from .store import _fold
        event = {"type": etype, "timestamp": self._now(), "data": data,
                 "seq": len(self.store.all()) + len(self._pending) + 1}
        self._pending.append(event)
        _fold(self.state, event)
        return event

    def _begin(self, key: str | None):
        """幂等控制：返回既有回执，或 None 表示首次执行。"""
        if key and key in self.state.receipts:
            return self.state.receipts[key]["result"]
        return None

    def _commit(self) -> list[dict]:
        events = self.store.append_many(self._pending)
        self._pending = []
        return events

    def _finish(self, key: str | None, result: dict, _events=None) -> dict:
        if key:
            biz_seqs = [e["seq"] for e in self._pending]
            self._append("ReceiptRegistered",
                         {"key": key, "result": result, "events": biz_seqs})
        committed = self._commit()
        if not key:
            result = dict(result)
            result["_seqs"] = [e["seq"] for e in committed]
        return result

    def _material_cover(self, sku: str) -> dict[str, int]:
        """未开始各团的材料覆盖配额（先确认先得）。

        可用池 = 总库存 - 已开始/已完成的消耗；按确认先后逐团扣减。
        某团开场只到场一部分时，退回的配额自然留给后面的团。
        """
        item = self.state.stock.get(sku)
        if item is None:
            return {}
        remaining = item.total - item.used
        cover: dict[str, int] = {}
        for s in sorted(self.state.active_sessions(), key=lambda x: x.confirm_order):
            if s.material_sku != sku or s.status == dm.ST_IN_PROGRESS:
                continue
            got = max(0, min(s.size, remaining))
            cover[s.session_id] = got
            remaining -= got
        return cover

    def _session(self, sid: str) -> dm.Session:
        try:
            return self.state.sessions[sid]
        except KeyError:
            raise ServiceError("SESSION_NOT_FOUND", f"会话不存在: {sid}")

    def _incident(self, code: str, detail: dict, session_id: str | None = None) -> None:
        self._append("IncidentLogged",
                     {"code": code, "session_id": session_id, "detail": detail})

    # ---- 主数据维护 ------------------------------------------------------

    def register_course(self, key: str, course_code: str, version: int, title: str,
                        maker_skill: str, material_sku: str, *, guides: int = 1,
                        per_maker_ratio: int = 6, max_group_size: int = 30,
                        venue_kind: str = dm.VENUE_ANY, alt_skus=(),
                        pattern=((0, 0),), unit_fee_cents: int = 0) -> dict:
        with self.lock:
            cached = self._begin(key)
            if cached:
                return cached
            if venue_kind not in dm.VENUE_KINDS:
                raise ServiceError("BAD_VENUE_KIND", f"场地属性非法: {venue_kind}")
            exists = self.state.courses.get(course_code, {}).get(version)
            if exists:
                raise ServiceError("COURSE_VERSION_EXISTS",
                                   f"课程版本已存在: {course_code}@v{version}")
            data = {"course_code": course_code, "version": version, "title": title,
                    "maker_skill": maker_skill, "material_sku": material_sku,
                    "guides": guides, "per_maker_ratio": per_maker_ratio,
                    "max_group_size": max_group_size, "venue_kind": venue_kind,
                    "alt_skus": list(alt_skus), "pattern": [list(p) for p in pattern],
                    "unit_fee_cents": unit_fee_cents}
            ev = self._append("CourseRegistered", data)
            return self._finish(key, {"course": f"{course_code}@v{version}", "seq": ev["seq"]}, [ev])

    def register_venue(self, key: str, venue_id: str, name: str,
                       indoor: bool, capacity: int) -> dict:
        with self.lock:
            cached = self._begin(key)
            if cached:
                return cached
            if venue_id in self.state.venues:
                raise ServiceError("VENUE_EXISTS", f"场地已登记: {venue_id}")
            ev = self._append("VenueRegistered", {"venue_id": venue_id, "name": name,
                                                  "indoor": indoor, "capacity": capacity})
            return self._finish(key, {"venue_id": venue_id, "seq": ev["seq"]}, [ev])

    def register_master(self, key: str, master_id: str, name: str, skills) -> dict:
        with self.lock:
            cached = self._begin(key)
            if cached:
                return cached
            if master_id in self.state.masters:
                raise ServiceError("MASTER_EXISTS", f"师傅已登记: {master_id}")
            ev = self._append("MasterRegistered", {"master_id": master_id, "name": name,
                                                   "skills": sorted(skills)})
            return self._finish(key, {"master_id": master_id, "seq": ev["seq"]}, [ev])

    def register_leave(self, key: str, master_id: str, day: str, slot: int,
                       reason: str = "") -> dict:
        """师傅临时请假：登记后自动尝试改派受影响会话，改派不了给异常。"""
        with self.lock:
            cached = self._begin(key)
            if cached:
                return cached
            if master_id not in self.state.masters:
                raise ServiceError("MASTER_NOT_FOUND", f"师傅不存在: {master_id}")
            ev = self._append("MasterLeaveRegistered",
                              {"master_id": master_id, "date": day, "slot": slot,
                               "reason": reason})
            effects = self._heal_leave(master_id, day, slot)
            result = {"master_id": master_id, "date": day, "slot": slot,
                      "reassigned": effects, "seq": ev["seq"]}
            return self._finish(key, result, [ev])

    def cancel_leave(self, key: str, master_id: str, day: str, slot: int) -> dict:
        with self.lock:
            cached = self._begin(key)
            if cached:
                return cached
            ev = self._append("MasterLeaveCancelled",
                              {"master_id": master_id, "date": day, "slot": slot})
            return self._finish(key, {"seq": ev["seq"]}, [ev])

    def _heal_leave(self, master_id: str, day: str, slot: int) -> list[dict]:
        """请假后扫描受影响会话：已开始的锁定不动，未开始的尝试改派。"""
        sk = dm.slot_key(day, slot)
        effects = []
        for s in sorted(self.state.active_sessions(),
                        key=lambda x: (x.status != dm.ST_IN_PROGRESS, x.confirm_order)):
            staff = s.staff_by_slot.get(sk)
            if not staff:
                continue
            roles = [role for role, mid in staff.items() if mid == master_id]
            for role in roles:
                skill = dm.GUIDE_SKILL if role == dm.GUIDE_SKILL \
                    else self.state.course(s.course_code, s.version).maker_skill
                repl = engine.find_replacement(self.state, s, sk, skill)
                if repl is not None:
                    ev = self._append("MakersReassigned",
                                      {"session_id": s.session_id, "reason": "MASTER_LEAVE",
                                       "staff_by_slot": {sk: {role: repl.master_id}}})
                    self._incident("STAFF_REASSIGNED",
                                   {"slot_key": sk, "role": role,
                                    "from": master_id, "to": repl.master_id},
                                   s.session_id)
                    effects.append({"session_id": s.session_id, "role": role,
                                    "replacement": repl.master_id})
                else:
                    self._incident(dm.INC_STAFF_SHORTAGE,
                                   {"slot_key": sk, "role": role, "missing_master": master_id,
                                    "started": s.status == dm.ST_IN_PROGRESS},
                                   s.session_id)
                    effects.append({"session_id": s.session_id, "role": role,
                                    "replacement": None})
        return effects

    def receive_stock(self, key: str, sku: str, qty: int) -> dict:
        with self.lock:
            cached = self._begin(key)
            if cached:
                return cached
            if qty <= 0:
                raise ServiceError("BAD_QTY", "入库数量必须为正")
            ev = self._append("StockReceived", {"sku": sku, "qty": qty})
            return self._finish(key, {"sku": sku, "qty": qty, "seq": ev["seq"]}, [ev])

    def adjust_stock(self, key: str, sku: str, delta: int, reason: str = "") -> dict:
        """材料临时短缺用负向调整登记；若跌破已预留量，逐团给出可解释异常。"""
        with self.lock:
            cached = self._begin(key)
            if cached:
                return cached
            item = self.state.stock.get(sku)
            if item is None or item.total + delta < item.used:
                raise ServiceError("STOCK_NEGATIVE",
                                   "调整后库存不能覆盖已消耗材料",
                                   {"total": item.total if item else 0,
                                    "consumed": item.used if item else 0})
            ev = self._append("StockAdjusted", {"sku": sku, "delta": delta, "reason": reason})
            shortages = []
            if delta < 0:
                item = self.state.stock[sku]
                if item.total < item.used:
                    # 理论上不会进入（上面已拦截），保险起见直接报告
                    pass
                # 预留超出可用：按确认先后给出各团覆盖配额并标记缺口
                cover = self._material_cover(sku)
                for s in sorted(self.state.active_sessions(),
                                key=lambda x: x.confirm_order):
                    if s.material_sku != sku or s.status == dm.ST_IN_PROGRESS:
                        continue
                    got = cover.get(s.session_id, 0)
                    if got < s.size:
                        self._incident(dm.INC_MATERIAL_SHORT,
                                       {"sku": sku, "need": s.size, "covered": got,
                                        "short": s.size - got}, s.session_id)
                        shortages.append({"session_id": s.session_id,
                                          "need": s.size, "covered": got})
            return self._finish(key, {"sku": sku, "delta": delta,
                                      "shortages": shortages, "seq": ev["seq"]}, [ev])

    # ---- 团队需求与排程 --------------------------------------------------

    def receive_team(self, key: str, booking_id: str | None, course_code: str,
                     start_date: str, start_slot: int, students: int,
                     *, channel: str = "onsite", note: str = "",
                     version_pref: int | None = None,
                     rain_indoor: bool = False) -> dict:
        with self.lock:
            cached = self._begin(key)
            if cached:
                return cached
            try:
                cv = self.state.course(course_code, version_pref)
            except KeyError:
                raise ServiceError("COURSE_NOT_FOUND",
                                   f"课程或版本不存在: {course_code}")
            if students <= 0:
                raise ServiceError("BAD_STUDENTS", "学生数必须为正")
            if not (0 <= start_slot < dm.SLOTS_PER_DAY):
                raise ServiceError("BAD_SLOT", f"时段非法: {start_slot}")
            try:
                dm.expand_slots(start_date, start_slot, cv.pattern)  # 校验跨日模式
            except ValueError as exc:
                raise ServiceError("BAD_PATTERN", str(exc))
            if booking_id is None:
                booking_id = self._new_id("BKG")
            if booking_id in self.state.bookings:
                raise ServiceError("BOOKING_EXISTS", f"团队需求已存在: {booking_id}")

            booking = {"booking_id": booking_id, "channel": channel,
                       "course_code": course_code, "version_pref": version_pref,
                       "start_date": start_date, "start_slot": start_slot,
                       "students": students, "note": note}
            self._append("BookingReceived", booking)
            plan = engine.propose_plan(self.state, booking, indoor_only=rain_indoor)
            self._append("PlanProposed", {
                k: plan[k] for k in
                ("plan_id", "booking_id", "feasible", "total_students",
                 "covered", "shortage", "options", "reasons", "alternatives")})
            return self._finish(key, self.plan_view(plan["plan_id"]))

    def confirm_plan(self, key: str, plan_id: str,
                     roster_by_option: dict[str, list[str]] | None = None) -> dict:
        with self.lock:
            cached = self._begin(key)
            if cached:
                return cached
            plan = self.state.plans.get(plan_id)
            if plan is None:
                raise ServiceError("PLAN_NOT_FOUND", f"方案不存在: {plan_id}")
            if plan.get("confirmed"):
                raise ServiceError("PLAN_ALREADY_CONFIRMED", f"方案已确认: {plan_id}")
            if not plan["feasible"]:
                raise ServiceError("PLAN_NOT_FEASIBLE", "方案未全员可行，不能确认",
                                   {"reasons": plan["reasons"],
                                    "alternatives": plan["alternatives"]})

            options = []
            auto_idx = 0
            for opt in plan["options"]:
                codes = (roster_by_option or {}).get(opt["option_id"])
                if not codes:
                    codes = [f"{plan['booking_id']}-P{auto_idx + i + 1:03d}"
                             for i in range(opt["size"])]
                    auto_idx += opt["size"]
                if len(codes) != opt["size"]:
                    raise ServiceError("ROSTER_SIZE_MISMATCH",
                                       f"名单人数 {len(codes)} 与组人数 {opt['size']} 不符",
                                       {"option_id": opt["option_id"]})
                options.append({**opt, "roster_codes": codes})

            # 确认前再校验一次硬锁定资源（防止同锁内并发语义外的过期方案）
            self._guard_against_started(options)
            ev = self._append("PlanConfirmed", {"plan_id": plan_id,
                                                "booking_id": plan["booking_id"],
                                                "options": options})
            return self._finish(key, {"plan_id": plan_id, "confirmed": True,
                                      "sessions": [o["session_id"] for o in options],
                                      "seq": ev["seq"]}, [ev])

    def _guard_against_started(self, new_options: list[dict]) -> None:
        """确认前复核：任何新分组都不能超过容量或占用已开始体验的资源。

        usage/busy/free 已包含所有未终结会话（含 IN_PROGRESS 的硬锁定）。
        """
        venue_tent: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        staff_tent: dict[str, set[str]] = defaultdict(set)
        stock_tent: dict[str, int] = defaultdict(int)
        usage = engine.venue_usage(self.state)
        busy = engine.staff_busy(self.state)
        for opt in new_options:
            for sk, vid in opt["venue_by_slot"].items():
                v = self.state.venues[vid]
                occ = usage.get(sk, {}).get(vid, 0) + venue_tent[sk][vid]
                if occ + opt["size"] > v.capacity:
                    raise ServiceError("STARTED_RESOURCE_PROTECTED",
                                       "场地容量不足，已开始体验的占用受保护",
                                       {"slot_key": sk, "venue_id": vid})
                venue_tent[sk][vid] += opt["size"]
            for sk, staff in opt["staff_by_slot"].items():
                ids = set(staff.values())
                if ids & set(busy.get(sk, {})) or ids & staff_tent[sk]:
                    raise ServiceError("STARTED_RESOURCE_PROTECTED",
                                       "师傅已被占用（含已开始体验的指导岗位）",
                                       {"slot_key": sk})
                staff_tent[sk] |= ids
            sku = opt["material_sku"]
            if engine.stock_free(self.state, sku, stock_tent) < opt["size"]:
                item = self.state.stock.get(sku)
                raise ServiceError("STARTED_RESOURCE_PROTECTED",
                                   "可用材料不足，不能抢占已开始体验的材料",
                                   {"sku": sku,
                                    "free": item.free if item else 0,
                                    "need": opt["size"]})
            stock_tent[sku] += opt["size"]

    # ---- 名单（最小信息：仅学生编号）------------------------------------

    def save_roster(self, key: str, session_id: str, student_codes: list[str]) -> dict:
        with self.lock:
            cached = self._begin(key)
            if cached:
                return cached
            s = self._session(session_id)
            if s.status not in (dm.ST_CONFIRMED, dm.ST_LATE):
                raise ServiceError("ROSTER_LOCKED",
                                   "体验已开始或结束，名单不可整体替换",
                                   {"status": s.status})
            if len(student_codes) != s.size:
                raise ServiceError("ROSTER_SIZE_MISMATCH",
                                   f"名单人数 {len(student_codes)} 与组人数 {s.size} 不符")
            if len(set(student_codes)) != len(student_codes):
                raise ServiceError("ROSTER_DUPLICATE", "名单内学生编号重复")
            ev = self._append("RosterSaved", {"session_id": session_id,
                                              "student_codes": student_codes})
            return self._finish(key, {"session_id": session_id,
                                      "students": len(student_codes), "seq": ev["seq"]}, [ev])

    # ---- 现场状态流转 ----------------------------------------------------

    def mark_late(self, key: str, session_id: str, note: str = "") -> dict:
        with self.lock:
            cached = self._begin(key)
            if cached:
                return cached
            s = self._session(session_id)
            if s.status != dm.ST_CONFIRMED:
                raise ServiceError("BAD_STATUS", "仅已确认未开始的团可标记迟到",
                                   {"status": s.status})
            ev = self._append("TeamLate", {"session_id": session_id, "note": note})
            # 迟到不释放资源：软预留保留，晚到仍有材料和指导
            return self._finish(key, {"session_id": session_id,
                                      "status": dm.ST_LATE, "seq": ev["seq"]}, [ev])

    def start_session(self, key: str, session_id: str,
                      attended_codes: list[str] | None = None) -> dict:
        """开场：校验指导与材料，资源由软预留转硬锁定（消耗）。"""
        with self.lock:
            cached = self._begin(key)
            if cached:
                return cached
            s = self._session(session_id)
            if s.status not in (dm.ST_CONFIRMED, dm.ST_LATE):
                raise ServiceError("BAD_STATUS", "当前状态不能开场",
                                   {"status": s.status})

            # 指导岗位：只校验首个时段（此刻），缺口尝试改派
            first_sk = dm.slot_key(*s.slots[0])
            self._ensure_staff_or_heal(s, first_sk)

            attended = set(attended_codes) if attended_codes is not None else set(s.roster)
            unknown = attended - set(s.roster)
            if unknown:
                raise ServiceError("ROSTER_UNKNOWN", "到场编号不在名单中",
                                   {"codes": sorted(unknown)})
            n_attended = len(attended)

            # 材料：按确认先后在"未消耗总量"内逐团分配覆盖配额；
            # 已开始的消耗先扣，晚确认的团承担缺口（先确认先得）。
            cover = self._material_cover(s.material_sku)
            covered = cover.get(session_id, 0)
            consumed = min(n_attended, covered)
            if consumed <= 0:
                raise ServiceError("MATERIAL_INSUFFICIENT",
                                   "无任何可用材料包，不能开场", {"sku": s.material_sku})
            incidents = []
            if consumed < n_attended:
                self._incident(dm.INC_PARTIAL_KITS,
                               {"sku": s.material_sku, "attended": n_attended,
                                "kits": consumed, "short": n_attended - consumed},
                               session_id)
                incidents.append(dm.INC_PARTIAL_KITS)

            ev = self._append("SessionStarted",
                              {"session_id": session_id,
                               "attended_codes": sorted(attended),
                               "consumed_qty": consumed})
            result = {"session_id": session_id, "status": dm.ST_IN_PROGRESS,
                      "attended": n_attended, "consumed": consumed,
                      "incidents": incidents, "seq": ev["seq"]}
            return self._finish(key, result, [ev])

    def _ensure_staff_or_heal(self, s: dm.Session, sk: str) -> None:
        cv = self.state.course(s.course_code, s.version)
        d, slot = sk.split("|")
        assigned = s.staff_by_slot.get(sk, {})
        for role, mid in list(assigned.items()):
            m = self.state.masters.get(mid)
            if m and (d, int(slot)) not in m.leaves:
                continue
            skill = dm.GUIDE_SKILL if role == dm.GUIDE_SKILL else cv.maker_skill
            repl = engine.find_replacement(self.state, s, sk, skill)
            if repl is None:
                raise ServiceError("STAFF_UNAVAILABLE",
                                   "师傅请假且无人可改派，暂缓开场",
                                   {"session_id": s.session_id, "role": role,
                                    "missing_master": mid})
            self._append("MakersReassigned",
                         {"session_id": s.session_id, "reason": "START_HEAL",
                          "staff_by_slot": {sk: {role: repl.master_id}}})
            self._incident("STAFF_REASSIGNED",
                           {"slot_key": sk, "role": role,
                            "from": mid, "to": repl.master_id}, s.session_id)

        # 岗位数量复核（讲解员 + 按人数配比的制坯师）
        after = self.state.sessions[s.session_id].staff_by_slot.get(sk, {})
        roles = list(after.values())
        present = [mid for mid in roles
                   if (d, int(slot)) not in self.state.masters[mid].leaves]
        n_need = cv.guides + math.ceil(s.size / cv.per_maker_ratio)
        if len(present) != n_need:
            raise ServiceError("STAFF_UNAVAILABLE",
                               "指导岗位不足，暂缓开场",
                               {"need": n_need, "present": len(present)})

    def complete_session(self, key: str, session_id: str) -> dict:
        with self.lock:
            cached = self._begin(key)
            if cached:
                return cached
            s = self._session(session_id)
            if s.status != dm.ST_IN_PROGRESS:
                raise ServiceError("BAD_STATUS", "仅进行中的体验可正常结束",
                                   {"status": s.status})
            settlement = self._settlement(s)
            ev = self._append("SessionCompleted",
                              {"session_id": session_id, "settlement": settlement})
            return self._finish(key, {"session_id": session_id,
                                      "status": dm.ST_COMPLETED,
                                      "settlement": settlement, "seq": ev["seq"]}, [ev])

    def cancel_session(self, key: str, session_id: str, reason: str = "") -> dict:
        """开始前取消：释放全部软预留。"""
        with self.lock:
            cached = self._begin(key)
            if cached:
                return cached
            s = self._session(session_id)
            if s.status not in (dm.ST_CONFIRMED, dm.ST_LATE):
                raise ServiceError("BAD_STATUS", "已开始的体验不能取消，请用提前终止",
                                   {"status": s.status})
            ev = self._append("SessionCancelled",
                              {"session_id": session_id, "reason": reason})
            return self._finish(key, {"session_id": session_id,
                                      "status": dm.ST_CANCELLED, "seq": ev["seq"]}, [ev])

    def terminate_session(self, key: str, session_id: str, reason: str = "") -> dict:
        """开始后提前终止：按实际到场与已耗材料结算，不收回资源。"""
        with self.lock:
            cached = self._begin(key)
            if cached:
                return cached
            s = self._session(session_id)
            if s.status != dm.ST_IN_PROGRESS:
                raise ServiceError("BAD_STATUS", "仅进行中的体验可提前终止",
                                   {"status": s.status})
            settlement = self._settlement(s, note=reason)
            ev = self._append("SessionTerminated",
                              {"session_id": session_id, "reason": reason,
                               "settlement": settlement})
            return self._finish(key, {"session_id": session_id,
                                      "status": dm.ST_TERMINATED,
                                      "settlement": settlement, "seq": ev["seq"]}, [ev])

    def _settlement(self, s: dm.Session, note: str = "") -> dict:
        cv = self.state.course(s.course_code, s.version)
        item = self.state.stock.get(s.material_sku)
        consumed = item.consumed.get(s.session_id, 0) if item else 0
        return {
            "basis": "ATTENDED",
            "students_planned": s.size, "students_attended": s.attended_count,
            "unit_fee_cents": cv.unit_fee_cents,
            "fee_cents": s.attended_count * cv.unit_fee_cents,
            "material_sku": s.material_sku, "material_consumed": consumed,
            "note": note,
        }

    # ---- 拆团 ------------------------------------------------------------

    def split_team(self, key: str, session_id: str, groups: list[dict]) -> dict:
        """开场前拆团：groups 为 [{'size': n, 'course_code'?:..., 'rain_indoor'?:...}]。

        原子操作：任一子组排不下则整体失败，父团不动。
        """
        with self.lock:
            cached = self._begin(key)
            if cached:
                return cached
            parent = self._session(session_id)
            if parent.status not in (dm.ST_CONFIRMED, dm.ST_LATE):
                raise ServiceError("BAD_STATUS", "仅未开始的团可拆",
                                   {"status": parent.status})
            if sum(g["size"] for g in groups) != parent.size:
                raise ServiceError("SPLIT_SIZE_MISMATCH",
                                   "拆团后人数之和须等于原团人数")

            usage = engine.venue_usage(self.state, frozenset({parent.session_id}))
            busy = engine.staff_busy(self.state, frozenset({parent.session_id}))
            from collections import defaultdict
            tv: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
            ts: dict[str, set[str]] = defaultdict(set)
            tk: dict[str, int] = defaultdict(int)

            children_plan, codes = [], list(parent.roster)
            cursor = 0
            for i, g in enumerate(groups):
                gsize = g["size"]
                cv = self.state.course(g.get("course_code", parent.course_code),
                                       g.get("version", parent.version))
                slots = dm.expand_slots(g.get("start_date", parent.slots[0][0]),
                                        g.get("start_slot", parent.slots[0][1]),
                                        cv.pattern)
                got = engine.assign_group(
                    self.state, cv, slots, gsize, usage, busy, tv, ts, tk,
                    indoor_only=g.get("rain_indoor", False))
                if got is None:
                    raise ServiceError("SPLIT_NOT_FEASIBLE",
                                       f"第 {i + 1} 子组资源不足，拆团未执行",
                                       {"group_no": i + 1})
                cid = self._new_id("SES")
                children_plan.append({
                    "option_id": f"{parent.option_id}-S{i + 1}", "group_no": i + 1,
                    "session_id": cid, "size": gsize,
                    "course_code": cv.course_code, "version": cv.version,
                    "slots": slots, "roster_codes": codes[cursor:cursor + gsize], **got,
                })
                cursor += gsize
                for sk2, vid in got["venue_by_slot"].items():
                    tv[sk2][vid] += gsize
                for sk2, staff in got["staff_by_slot"].items():
                    ts[sk2].update(staff.values())
                tk[got["material_sku"]] += gsize

            ev = self._append("TeamSplit",
                              {"parent_id": parent.session_id, "children": children_plan})
            return self._finish(key, {"parent_id": parent.session_id,
                                      "status": dm.ST_SPLIT,
                                      "children": [c["session_id"] for c in children_plan],
                                      "seq": ev["seq"]}, [ev])

    # ---- 雨天转室 --------------------------------------------------------

    def rain_switch(self, key: str, day: str, slot: int) -> dict:
        """某时段下雨：未开始的室外团转室内；已开始的不挪动（硬锁定）。"""
        with self.lock:
            cached = self._begin(key)
            if cached:
                return cached
            sk = dm.slot_key(day, slot)
            moved, blocked, protected = [], [], []
            for s in sorted(self.state.active_sessions(), key=lambda x: x.confirm_order):
                if sk not in s.venue_by_slot:
                    continue
                if s.status == dm.ST_IN_PROGRESS:
                    self._incident(dm.INC_RAIN_PROTECTED,
                                   {"slot_key": sk, "venue_id": s.venue_by_slot[sk]},
                                   s.session_id)
                    protected.append(s.session_id)
                    continue
                v = self.state.venues.get(s.venue_by_slot[sk])
                if v and v.indoor:
                    continue
                target = engine.find_indoor_venue(self.state, s, sk)
                if target is None:
                    self._incident(dm.INC_RAIN_NO_ROOM,
                                   {"slot_key": sk, "size": s.size}, s.session_id)
                    blocked.append(s.session_id)
                    continue
                self._append("SessionRelocated",
                             {"session_id": s.session_id, "slot_key": sk,
                              "from_venue": s.venue_by_slot[sk], "venue_id": target.venue_id})
                self._incident(dm.INC_RAIN_RELOCATED,
                               {"slot_key": sk, "to_venue": target.venue_id}, s.session_id)
                moved.append({"session_id": s.session_id, "venue_id": target.venue_id})
            return self._finish(key, {"slot_key": sk, "moved": moved,
                                      "no_room": blocked, "protected": protected}, [])

    # ---- 查询 / 复盘 / 恢复 ---------------------------------------------

    def plan_view(self, plan_id: str) -> dict:
        plan = self.state.plans[plan_id]
        return {
            "plan_id": plan_id, "booking_id": plan["booking_id"],
            "feasible": plan["feasible"], "confirmed": plan.get("confirmed", False),
            "total_students": plan["total_students"], "covered": plan["covered"],
            "shortage": plan["shortage"],
            "options": [{
                "option_id": o["option_id"], "group_no": o["group_no"],
                "session_id": o["session_id"], "size": o["size"],
                "course": f"{o['course_code']}@v{o['version']}",
                "slots": [list(x) for x in o["slots"]],
                "venues": o["venue_by_slot"], "staff": o["staff_by_slot"],
                "material_sku": o["material_sku"],
            } for o in plan["options"]],
            "reasons": plan["reasons"], "alternatives": plan.get("alternatives", []),
        }

    def session_view(self, session_id: str) -> dict:
        s = self._session(session_id)
        return {
            "session_id": s.session_id, "booking_id": s.booking_id,
            "plan_id": s.plan_id, "course": f"{s.course_code}@v{s.version}",
            "status": s.status, "size": s.size, "attended": s.attended_count,
            "slots": [list(x) for x in s.slots],
            "venues": dict(s.venue_by_slot),
            "staff": {k: dict(v) for k, v in s.staff_by_slot.items()},
            "material_sku": s.material_sku, "held": s.held,
            "roster": [{"code": c, "attended": e.attended}
                       for c, e in sorted(s.roster.items())],
            "parent_id": s.parent_id, "child_ids": list(s.child_ids),
            "settlement": s.settlement,
        }

    def day_schedule(self, day: str) -> dict:
        rows = []
        for s in sorted(self.state.sessions_on(day), key=lambda x: x.confirm_order):
            rows.append({"session_id": s.session_id, "status": s.status,
                         "course": f"{s.course_code}@v{s.version}",
                         "size": s.size, "slots": [list(x) for x in s.slots
                                                   if x[0] == day],
                         "venues": {k: v for k, v in s.venue_by_slot.items()
                                    if k.startswith(day + "|")}})
        return {"date": day, "sessions": rows}

    def stock_view(self) -> dict:
        return {sku: {"total": it.total, "held": it.held, "consumed": it.used,
                      "free": it.free}
                for sku, it in sorted(self.state.stock.items())}

    def incidents(self, session_id: str | None = None) -> list[dict]:
        out = [dict(i) for i in self.state.incidents]
        if session_id:
            out = [i for i in out if i.get("session_id") == session_id]
        return out

    def replay_until(self, seq: int) -> dm.State:
        """复盘：重建截至指定事件序号（含）的状态。"""
        return replay(self.store.all(), until_seq=seq)

    def handover(self, day: str) -> dict:
        """系统恢复后交接：当天尚未结束的课程及其资源占用。"""
        items = []
        for s in sorted(self.state.sessions_on(day), key=lambda x: x.confirm_order):
            if s.status not in dm.ACTIVE_STATUSES:
                continue
            items.append({"session_id": s.session_id,
                          "status": s.status, "course": f"{s.course_code}@v{s.version}",
                          "size": s.size, "attended": s.attended_count,
                          "venues": {k: v for k, v in s.venue_by_slot.items()
                                     if k.startswith(day + "|")},
                          "staff": {k: dict(v) for k, v in s.staff_by_slot.items()
                                    if k.startswith(day + "|")},
                          "material_sku": s.material_sku})
        return {"date": day, "open_sessions": items,
                "stock": self.stock_view(), "incidents_today": [
                    i for i in self.state.incidents
                    if str(i.get("detail", {}).get("slot_key", "")).startswith(day + "|")]}


# 所有写命令统一包裹事务：成功整批提交，失败回滚内存状态
_WRITE_METHODS = (
    "register_course", "register_venue", "register_master",
    "register_leave", "cancel_leave", "receive_stock", "adjust_stock",
    "receive_team", "confirm_plan", "save_roster",
    "mark_late", "start_session", "complete_session",
    "cancel_session", "terminate_session", "split_team", "rain_switch",
)
for _name in _WRITE_METHODS:
    setattr(CapacityService, _name,
            _transactional(getattr(CapacityService, _name)))
