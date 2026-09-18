"""事件存储与回放（事件溯源）。

- 所有状态变更都是不可变事件，按 seq 顺序追加。
- 复盘时把事件流回放（可带时间上限）即可重建任意时点状态。
- 渠道重试/离线补录凭 Idempotency-Key 去重：重复键返回首次结果。
"""
from __future__ import annotations

import json
import os
import threading

from . import domain as dm


class EventStore:
    """JSONL 文件事件存储；生产环境可替换为数据库实现，接口保持一致。"""

    def __init__(self, path: str | None = None):
        self._lock = threading.RLock()
        self.path = path
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self._events = [json.loads(line) for line in f if line.strip()]
        else:
            self._events = []

    def append(self, event: dict) -> dict:
        with self._lock:
            event = dict(event)
            event["seq"] = len(self._events) + 1
            self._events.append(event)
            self._flush()
            return event

    def append_many(self, events: list[dict]) -> list[dict]:
        """整批原子提交：一次赋号、一次落盘（os.replace），崩溃不留半批。"""
        with self._lock:
            start = len(self._events)
            for i, event in enumerate(events):
                event["seq"] = start + i + 1
                self._events.append(event)
            self._flush()
            return list(events)

    def _flush(self) -> None:
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for e in self._events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._events)

    def truncate(self) -> None:
        """仅供测试/复盘沙箱使用。"""
        with self._lock:
            self._events = []
            if self.path and os.path.exists(self.path):
                os.remove(self.path)


# ---- 折叠器：事件 -> 状态 --------------------------------------------------

def _fold(state: dm.State, e: dict) -> None:
    t = e["type"]
    d = e["data"]

    if t == "CourseRegistered":
        cv = dm.CourseVersion(
            course_code=d["course_code"], version=d["version"], title=d["title"],
            maker_skill=d["maker_skill"], material_sku=d["material_sku"],
            guides=d.get("guides", 1), per_maker_ratio=d.get("per_maker_ratio", 6),
            max_group_size=d.get("max_group_size", 30),
            venue_kind=d.get("venue_kind", dm.VENUE_ANY),
            alt_skus=tuple(d.get("alt_skus", ())),
            pattern=tuple(tuple(p) for p in d.get("pattern", ((0, 0),))),
            unit_fee_cents=d.get("unit_fee_cents", 0),
        )
        state.courses.setdefault(cv.course_code, {})[cv.version] = cv

    elif t == "VenueRegistered":
        state.venues[d["venue_id"]] = dm.Venue(
            venue_id=d["venue_id"], name=d["name"],
            indoor=d["indoor"], capacity=d["capacity"])

    elif t == "MasterRegistered":
        state.masters[d["master_id"]] = dm.Master(
            master_id=d["master_id"], name=d["name"],
            skills=frozenset(d["skills"]), leaves=set(map(tuple, d.get("leaves", []))))

    elif t == "MasterLeaveRegistered":
        m = state.masters[d["master_id"]]
        m.leaves.add((d["date"], d["slot"]))

    elif t == "MasterLeaveCancelled":
        state.masters[d["master_id"]].leaves.discard((d["date"], d["slot"]))

    elif t == "StockReceived":
        state.stock_item(d["sku"]).received += d["qty"]

    elif t == "StockAdjusted":
        state.stock_item(d["sku"]).adjustments += d["delta"]

    elif t == "BookingReceived":
        state.bookings[d["booking_id"]] = {
            "booking_id": d["booking_id"], "channel": d.get("channel", "onsite"),
            "course_code": d["course_code"],
            "version_pref": d.get("version_pref"),
            "start_date": d["start_date"], "start_slot": d["start_slot"],
            "students": d["students"], "note": d.get("note", ""),
            "created_at": e["timestamp"],
        }

    elif t == "PlanProposed":
        state.plans[d["plan_id"]] = {
            "plan_id": d["plan_id"], "booking_id": d["booking_id"],
            "feasible": d["feasible"], "total_students": d["total_students"],
            "covered": d["covered"], "shortage": d["shortage"],
            "options": d["options"], "reasons": d.get("reasons", []),
            "alternatives": d.get("alternatives", []),
            "created_at": e["timestamp"],
        }

    elif t == "PlanConfirmed":
        plan = state.plans[d["plan_id"]]
        plan["confirmed"] = True
        plan["confirmed_at"] = e["timestamp"]
        for opt in d["options"]:
            cv = state.course(opt["course_code"], opt["version"])
            slots = [tuple(s) for s in opt["slots"]]
            s = dm.Session(
                session_id=opt["session_id"], booking_id=d["booking_id"],
                plan_id=d["plan_id"], option_id=opt["option_id"],
                course_code=opt["course_code"], version=opt["version"],
                group_no=opt["group_no"], size=opt["size"], slots=slots,
                venue_by_slot=dict(opt["venue_by_slot"]),
                staff_by_slot={k: dict(v) for k, v in opt["staff_by_slot"].items()},
                material_sku=opt["material_sku"], held=opt["held"],
                roster={c: dm.RosterEntry(code=c) for c in opt.get("roster_codes", [])},
                confirm_order=e["seq"],
            )
            state.sessions[s.session_id] = s
            item = state.stock_item(s.material_sku)
            item.holds[s.session_id] = item.holds.get(s.session_id, 0) + s.held

    elif t == "RosterSaved":
        s = state.sessions[d["session_id"]]
        codes = set(d["student_codes"])
        for code in d["student_codes"]:
            s.roster.setdefault(code, dm.RosterEntry(code=code))
        for extra in [c for c in s.roster if c not in codes]:
            del s.roster[extra]

    elif t == "TeamLate":
        state.sessions[d["session_id"]].status = dm.ST_LATE

    elif t == "SessionStarted":
        s = state.sessions[d["session_id"]]
        s.status = dm.ST_IN_PROGRESS
        attended = set(d.get("attended_codes", []))
        for code, entry in s.roster.items():
            entry.attended = code in attended if attended else True
        item = state.stock_item(s.material_sku)
        item.holds.pop(s.session_id, None)
        item.consumed[s.session_id] = d["consumed_qty"]

    elif t == "SessionCompleted":
        s = state.sessions[d["session_id"]]
        s.status = dm.ST_COMPLETED
        s.settlement = d["settlement"]

    elif t == "SessionCancelled":
        s = state.sessions[d["session_id"]]
        s.status = dm.ST_CANCELLED
        state.stock_item(s.material_sku).holds.pop(s.session_id, None)

    elif t == "SessionTerminated":
        s = state.sessions[d["session_id"]]
        s.status = dm.ST_TERMINATED
        s.settlement = d["settlement"]

    elif t == "TeamSplit":
        parent = state.sessions[d["parent_id"]]
        parent.status = dm.ST_SPLIT
        for ch in d["children"]:
            slots = [tuple(x) for x in ch["slots"]]
            child = dm.Session(
                session_id=ch["session_id"], booking_id=parent.booking_id,
                plan_id=parent.plan_id, option_id=ch["option_id"],
                course_code=ch["course_code"], version=ch["version"],
                group_no=ch["group_no"], size=ch["size"], slots=slots,
                venue_by_slot=dict(ch["venue_by_slot"]),
                staff_by_slot={k: dict(v) for k, v in ch["staff_by_slot"].items()},
                material_sku=ch["material_sku"], held=ch["held"],
                roster={c: dm.RosterEntry(code=c) for c in ch.get("roster_codes", [])},
                parent_id=parent.session_id, confirm_order=e["seq"],
            )
            parent.child_ids.append(child.session_id)
            state.sessions[child.session_id] = child
            item = state.stock_item(child.material_sku)
            item.holds[child.session_id] = item.holds.get(child.session_id, 0) + child.held
        state.stock_item(parent.material_sku).holds.pop(parent.session_id, None)

    elif t == "MakersReassigned":
        s = state.sessions[d["session_id"]]
        for sk, staff in d["staff_by_slot"].items():
            s.staff_by_slot.setdefault(sk, {}).update(staff)

    elif t == "SessionRelocated":
        s = state.sessions[d["session_id"]]
        s.venue_by_slot[d["slot_key"]] = d["venue_id"]

    elif t == "IncidentLogged":
        state.incidents.append({
            "code": d["code"], "session_id": d.get("session_id"),
            "detail": d.get("detail", {}), "at": e["timestamp"]})

    elif t == "ReceiptRegistered":
        state.receipts[d["key"]] = {
            "key": d["key"], "result": d["result"],
            "at": e["timestamp"], "events": d.get("events", [])}

    else:
        raise ValueError(f"未知事件类型: {t}")


def replay(events: list[dict], until_seq: int | None = None) -> dm.State:
    """从事件流折叠状态；until_seq 用于复盘回放（含该序号）。"""
    state = dm.State()
    applied = 0
    for e in events:
        if until_seq is not None and e["seq"] > until_seq:
            break
        state.events.append(e)
        _fold(state, e)
        applied += 1
    _rebuild_counters(state)
    state.next_order = applied + 1
    return state


def _rebuild_counters(state: dm.State) -> None:
    """ID 计数器未单独持久化，重建时从实体编号取最大值，避免恢复后重号。

    会话编号在方案提出时即预占，因此未确认方案中的 option.session_id 也要计入。
    """
    def consider(ident: str) -> None:
        if "-" not in ident:
            return
        prefix, _, tail = ident.rpartition("-")
        if tail.isdigit():
            state.counters[prefix] = max(state.counters.get(prefix, 0), int(tail))

    for ident in list(state.bookings) + list(state.plans):
        consider(ident)
    for plan in state.plans.values():
        for opt in plan.get("options", []):
            consider(opt["session_id"])
    for ident in state.sessions:
        consider(ident)
