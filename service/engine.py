"""排程引擎：在当前状态上做容量核算，产出事件与可解释结果。

约定：
- 引擎函数是纯函数：读状态、返回 (events, result)，不直接落库；
- 已开始(IN_PROGRESS)的体验对场地/师傅/材料享有硬锁定，
  任何新排程或改派都不能占用它们的资源；
- 所有不可行/异常都带原因码，写入 IncidentLogged 事件供复盘。
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, timedelta

from . import domain as dm


class DomainError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


# ---- 资源占用视图 ---------------------------------------------------------

def _slot_keys(session: dm.Session) -> list[str]:
    return [dm.slot_key(d, s) for d, s in session.slots]


def venue_usage(state: dm.State, exclude: frozenset[str] = frozenset()) -> dict[str, dict[str, int]]:
    """slot_key -> 场地 -> 已占人数（全部未终结会话；exclude 中的会话不计）。"""
    used: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for s in state.active_sessions():
        if s.session_id in exclude:
            continue
        for sk in _slot_keys(s):
            used[sk][s.venue_by_slot[sk]] += s.size
    return used


def staff_busy(state: dm.State, exclude: frozenset[str] = frozenset()) -> dict[str, dict[str, str]]:
    """slot_key -> 师傅id -> 会话id（请假者视为空缺，不占位）。"""
    busy: dict[str, dict[str, str]] = defaultdict(dict)
    for s in state.active_sessions():
        if s.session_id in exclude:
            continue
        for d, slot in s.slots:
            sk = dm.slot_key(d, slot)
            for _role, mid in s.staff_by_slot.get(sk, {}).items():
                master = state.masters.get(mid)
                if master and (d, slot) in master.leaves:
                    continue  # 请假留下空缺，由改派/事件解释
                busy[sk][mid] = s.session_id
    return busy


def free_masters(state: dm.State, skill: str, sk: str,
                 busy: dict[str, dict[str, str]],
                 tent: dict[str, set[str]],
                 exclude: set[str] | None = None) -> list[dm.Master]:
    d, slot = sk.split("|")
    exclude = exclude or set()
    out = []
    for m in state.masters.values():
        if skill not in m.skills:
            continue
        if (d, int(slot)) in m.leaves:
            continue
        if m.master_id in busy.get(sk, {}):
            continue
        if m.master_id in tent.get(sk, set()):
            continue
        if m.master_id in exclude:
            continue
        out.append(m)
    return sorted(out, key=lambda m: m.master_id)


def stock_free(state: dm.State, sku: str, tent_hold: dict[str, int]) -> int:
    item = state.stock.get(sku)
    free = item.free if item else 0
    return free - tent_hold.get(sku, 0)


def find_replacement(state: dm.State, session: dm.Session, sk: str,
                     skill: str) -> dm.Master | None:
    """为会话某时段找一名空闲且具备技能的替补师傅（不得动用已被占用者）。"""
    busy = staff_busy(state)
    pool = free_masters(state, skill, sk, busy, defaultdict(set))
    return pool[0] if pool else None


def find_indoor_venue(state: dm.State, session: dm.Session, sk: str) -> dm.Venue | None:
    """雨天转室：在室内馆中为会话寻找容量足够的场地。

    调用方在每团转室成功后立即折叠 SessionRelocated，因此已转入的占用
    直接体现在 venue_usage 中；会话自身在原室外场的占用通过 exclude 不计。
    """
    usage = venue_usage(state, frozenset({session.session_id}))
    candidates = []
    for v in state.venues.values():
        if not v.indoor:
            continue
        if usage.get(sk, {}).get(v.venue_id, 0) + session.size <= v.capacity:
            candidates.append(v)
    return sorted(candidates, key=lambda v: v.venue_id)[0] if candidates else None


# ---- 单组资源匹配 ---------------------------------------------------------

def _venue_matches(kind: str, indoor: bool) -> bool:
    if kind == dm.VENUE_ANY:
        return True
    if kind == dm.VENUE_INDOOR:
        return indoor
    return not indoor


def assign_group(state: dm.State, cv: dm.CourseVersion,
                 slots: list[tuple[str, int]], size: int,
                 usage: dict[str, dict[str, int]],
                 busy: dict[str, dict[str, str]],
                 tent_venue: dict[str, dict[str, int]],
                 tent_staff: dict[str, set[str]],
                 tent_stock: dict[str, int],
                 indoor_only: bool = False) -> dict | None:
    """为一个学生组找齐场地/师傅/材料；返回分配方案或 None。"""
    venues_pick: dict[str, str] = {}
    staff_pick: dict[str, dict[str, str]] = {}

    n_makers = math.ceil(size / cv.per_maker_ratio)
    for d, slot in slots:
        sk = dm.slot_key(d, slot)

        # 场地：要求室内时只挑室内馆；否则按课程场地属性
        candidates = []
        for v in state.venues.values():
            if not _venue_matches(cv.venue_kind, v.indoor):
                continue
            if indoor_only and not v.indoor:
                continue
            occupied = usage.get(sk, {}).get(v.venue_id, 0)
            occupied += tent_venue.get(sk, {}).get(v.venue_id, 0)
            if occupied + size <= v.capacity:
                candidates.append(v)
        if not candidates:
            return None
        venue = sorted(candidates, key=lambda v: (not v.indoor, v.venue_id))[0]
        venues_pick[sk] = venue.venue_id

        # 讲解员 + 制坯师：同一场内一人不可兼两岗
        chosen: set[str] = set()
        picks: dict[str, str] = {}
        for skill, n in ((dm.GUIDE_SKILL, cv.guides), (cv.maker_skill, n_makers)):
            pool = free_masters(state, skill, sk, busy, tent_staff, exclude=chosen)
            if len(pool) < n:
                return None
            for m in pool[:n]:
                picks[skill + ("" if skill == dm.GUIDE_SKILL else f"#{len(chosen)}")] = m.master_id
                chosen.add(m.master_id)
        staff_pick[sk] = picks

    # 材料：默认包优先，其次按登记顺序挑替代包
    sku_choices = (cv.material_sku,) + tuple(cv.alt_skus)
    sku = next((k for k in sku_choices if stock_free(state, k, tent_stock) >= size), None)
    if sku is None:
        return None

    return {"venue_by_slot": venues_pick, "staff_by_slot": staff_pick,
            "material_sku": sku, "held": size}


# ---- 团队需求 -> 可执行方案 ------------------------------------------------

def _new_id(state: dm.State, prefix: str) -> str:
    n = state.counters.get(prefix, 0) + 1
    state.counters[prefix] = n
    return f"{prefix}-{n:04d}"


def propose_plan(state: dm.State, booking: dict, indoor_only: bool = False) -> dict:
    """按课程版本与人数分组，给出可执行方案；不足部分给原因与替代时段。"""
    cv = state.course(booking["course_code"], booking.get("version_pref"))
    students = booking["students"]
    n_groups = math.ceil(students / cv.max_group_size) or 1
    base = [students // n_groups + (1 if i < students % n_groups else 0)
            for i in range(n_groups)]

    slots = dm.expand_slots(booking["start_date"], booking["start_slot"], cv.pattern)

    usage, busy = venue_usage(state), staff_busy(state)
    tent_venue: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    tent_staff: dict[str, set[str]] = defaultdict(set)
    tent_stock: dict[str, int] = defaultdict(int)

    options, reasons, covered = [], [], 0
    plan_id = _new_id(state, "PLAN")
    for i, gsize in enumerate(base):
        if gsize <= 0:
            continue
        got = assign_group(state, cv, slots, gsize, usage, busy,
                           tent_venue, tent_staff, tent_stock, indoor_only)
        if got is None:
            probe = _shortage_reasons(state, cv, slots, gsize, indoor_only,
                                      tent_venue, tent_staff, tent_stock)
            reasons.append({"group_no": i + 1, "size": gsize, "reasons": probe})
            continue
        sid = _new_id(state, "SES")
        options.append({
            "option_id": f"{plan_id}-G{i + 1}", "group_no": i + 1,
            "size": gsize, "session_id": sid,
            "course_code": cv.course_code, "version": cv.version,
            "slots": slots, **got,
        })
        covered += gsize
        for sk, vid in got["venue_by_slot"].items():
            tent_venue[sk][vid] += gsize
        for sk, staff in got["staff_by_slot"].items():
            tent_staff[sk].update(staff.values())
        tent_stock[got["material_sku"]] += gsize

    alternatives = []
    if covered < students:
        alternatives = _alternative_slots(state, cv, students, booking, indoor_only)

    return {
        "plan_id": plan_id, "booking_id": booking["booking_id"],
        "feasible": covered == students, "total_students": students,
        "covered": covered, "shortage": students - covered,
        "options": options, "reasons": reasons, "alternatives": alternatives,
    }


def _shortage_reasons(state, cv, slots, gsize, indoor_only,
                      tent_venue, tent_staff, tent_stock) -> list[str]:
    out = []
    usage, busy = venue_usage(state), staff_busy(state)
    # 场地
    venue_ok = False
    for d, slot in slots:
        sk = dm.slot_key(d, slot)
        for v in state.venues.values():
            if not _venue_matches(cv.venue_kind, v.indoor):
                continue
            if indoor_only and not v.indoor:
                continue
            occupied = usage.get(sk, {}).get(v.venue_id, 0) \
                + tent_venue.get(sk, {}).get(v.venue_id, 0)
            if occupied + gsize <= v.capacity:
                venue_ok = True
                break
    if not venue_ok:
        out.append(dm.R_VENUE_FULL)
    # 讲解 / 制坯
    for d, slot in slots:
        sk = dm.slot_key(d, slot)
        if len(free_masters(state, dm.GUIDE_SKILL, sk, busy, tent_staff)) < cv.guides:
            out.append(dm.R_NO_GUIDE)
        n_makers = math.ceil(gsize / cv.per_maker_ratio)
        if len(free_masters(state, cv.maker_skill, sk, busy, tent_staff)) < n_makers:
            out.append(dm.R_NO_MAKER)
        break
    # 材料
    if all(stock_free(state, k, tent_stock) < gsize
           for k in (cv.material_sku,) + tuple(cv.alt_skus)):
        out.append(dm.R_MATERIAL)
    return sorted(set(out))


def _alternative_slots(state, cv, students, booking, indoor_only) -> list[dict]:
    """主时段排不下时，扫描同日其他时段及未来数天，给出全员可行的替代。"""
    found = []
    start = date.fromisoformat(booking["start_date"])
    for day_off in range(dm.PLAN_HORIZON_DAYS):
        for slot in range(dm.SLOTS_PER_DAY):
            day = (start + timedelta(days=day_off)).isoformat()
            if day_off == 0 and slot == booking["start_slot"]:
                continue
            alt_slots = dm.expand_slots(day, slot, cv.pattern)
            usage, busy = venue_usage(state), staff_busy(state)
            tv: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
            ts: dict[str, set[str]] = defaultdict(set)
            tk: dict[str, int] = defaultdict(int)
            n_groups = math.ceil(students / cv.max_group_size)
            ok = True
            for i in range(n_groups):
                gsize = students // n_groups + (1 if i < students % n_groups else 0)
                got = assign_group(state, cv, alt_slots, gsize, usage, busy,
                                   tv, ts, tk, indoor_only)
                if got is None:
                    ok = False
                    break
                for sk, vid in got["venue_by_slot"].items():
                    tv[sk][vid] += gsize
                for sk, staff in got["staff_by_slot"].items():
                    ts[sk].update(staff.values())
                tk[got["material_sku"]] += gsize
            if ok:
                found.append({"start_date": day, "start_slot": slot,
                              "label": dm.SLOT_LABELS[slot]})
            if len(found) >= 3:
                return found
    return found
