"""领域纯逻辑：分组、时段/师傅/材料的放置搜索。不直接写库，结果交给 core 落库。"""

from .util import hhmm, session_end_iso, session_start_iso


def split_groups(headcount: int, group_size_max: int):
    """把团队人数均衡拆成若干组，每组不超过课程上限。"""
    n = (headcount + group_size_max - 1) // group_size_max
    base, rem = divmod(headcount, n)
    return [base + 1 if i < rem else base for i in range(n)]


def reason(code: str, message: str, facts=None) -> dict:
    """可解释状态条目：机器可读 code + 人可读 message + 支撑事实。"""
    return {"code": code, "message": message, "facts": facts or {}}


class Tentative:
    """同一方案内的试占位：同一次规划里，前一组占掉的容量/师傅/材料对后一组立即可见。"""

    def __init__(self):
        self.slot_used = {}    # slot_id -> extra headcount
        self.materials = {}    # material_id -> qty
        self.master_busy = []  # (master_id, date, start_min, end_min)

    def add_placement(self, p):
        self.slot_used[p["slot"]["id"]] = self.slot_used.get(p["slot"]["id"], 0) + p["headcount"]
        for mid, qty in p["materials"].items():
            self.materials[mid] = self.materials.get(mid, 0) + qty
        for m in p["masters"]:
            self.master_busy.append((m["master_id"], p["date"], p["start_min"], p["end_min"]))


def slot_used_capacity(conn, slot_id: str, ignore_session_ids=()) -> int:
    rows = conn.execute(
        "SELECT COALESCE(SUM(headcount),0) AS used FROM sessions "
        "WHERE slot_id=? AND status IN ('confirmed','in_progress')",
        (slot_id,),
    ).fetchone()
    used = rows["used"]
    if ignore_session_ids:
        marks = ",".join("?" for _ in ignore_session_ids)
        row = conn.execute(
            f"SELECT COALESCE(SUM(headcount),0) AS used FROM sessions "
            f"WHERE slot_id=? AND status IN ('confirmed','in_progress') AND id IN ({marks})",
            (slot_id, *ignore_session_ids),
        ).fetchone()
        used -= row["used"]
    return used


def reserved_material(conn, material_id: str, ignore_session_ids=()) -> int:
    sql = ("SELECT COALESCE(SUM(qty_reserved),0) AS qty FROM session_materials "
           "WHERE material_id=? AND status='reserved'")
    args = [material_id]
    if ignore_session_ids:
        marks = ",".join("?" for _ in ignore_session_ids)
        sql += f" AND session_id NOT IN ({marks})"
        args.extend(ignore_session_ids)
    return conn.execute(sql, args).fetchone()["qty"]


def _master_on_leave(conn, master_id: str, start_iso: str, end_iso: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM master_leaves WHERE master_id=? AND start_at < ? AND end_at > ? LIMIT 1",
        (master_id, end_iso, start_iso),
    ).fetchone()
    return row is not None


def _master_busy_session_ids(conn, master_id: str, date: str, start_min: int, end_min: int):
    rows = conn.execute(
        "SELECT s.id FROM session_masters sm JOIN sessions s ON s.id = sm.session_id "
        "WHERE sm.master_id=? AND sm.status='assigned' AND s.date=? "
        "AND s.status IN ('confirmed','in_progress') AND s.start_min < ? AND s.end_min > ?",
        (master_id, date, end_min, start_min),
    ).fetchall()
    return {r["id"] for r in rows}


def find_masters(conn, skill_reqs: dict, date: str, start_min: int, end_min: int,
                 tentative: Tentative = None, ignore_session_ids=()):
    """按技能需求找师傅；返回 [{master_id, master_name, skill}] 或 (None, 原因列表)。"""
    chosen, reasons = [], []
    start_iso, end_iso = session_start_iso(date, start_min), session_end_iso(date, end_min)
    for skill, count in skill_reqs.items():
        rows = conn.execute(
            "SELECT id, name FROM masters WHERE status='active' AND instr(skills, ?) > 0 ORDER BY id",
            (f'"{skill}"',),
        ).fetchall()
        picked = 0
        for r in rows:
            if picked >= count:
                break
            if any(c["master_id"] == r["id"] for c in chosen):
                continue  # 一名师傅在同一组里只算一次
            if _master_on_leave(conn, r["id"], start_iso, end_iso):
                continue
            busy = _master_busy_session_ids(conn, r["id"], date, start_min, end_min) - set(ignore_session_ids)
            if busy:
                continue
            if tentative and any(
                t[0] == r["id"] and t[1] == date and t[2] < end_min and t[3] > start_min
                for t in tentative.master_busy
            ):
                continue
            chosen.append({"master_id": r["id"], "master_name": r["name"], "skill": skill})
            picked += 1
        if picked < count:
            reasons.append(reason(
                "MASTER_UNAVAILABLE",
                f"{date} {hhmm(start_min)}-{hhmm(end_min)} 缺少「{skill}」技能师傅：需要 {count} 名，可派 {picked} 名",
                {"skill": skill, "need": count, "available": picked, "date": date,
                 "start": hhmm(start_min), "end": hhmm(end_min)},
            ))
    if reasons:
        return None, reasons
    return chosen, []


def check_materials(conn, material_reqs: dict, headcount: int, tentative: Tentative = None,
                    ignore_session_ids=()):
    """返回 ({material_id: qty}, []) 或 (None, 原因列表)。"""
    need, reasons = {}, []
    for mid, per in material_reqs.items():
        qty = per * headcount
        row = conn.execute("SELECT stock, name, unit FROM materials WHERE id=?", (mid,)).fetchone()
        if row is None:
            reasons.append(reason("MATERIAL_UNKNOWN", f"材料 {mid} 未维护", {"material_id": mid}))
            continue
        available = row["stock"] - reserved_material(conn, mid, ignore_session_ids)
        if tentative:
            available -= tentative.materials.get(mid, 0)
        if qty > available:
            reasons.append(reason(
                "MATERIAL_SHORTAGE",
                f"材料「{row['name']}」不足：需要 {qty}{row['unit']}，可用 {available}{row['unit']}，缺口 {qty - available}{row['unit']}",
                {"material_id": mid, "material_name": row["name"], "need": qty,
                 "available": available, "gap": qty - available, "unit": row["unit"]},
            ))
        need[mid] = qty
    if reasons:
        return None, reasons
    return need, []


def search_placement(conn, course: dict, date: str, win_start: int, win_end: int, headcount: int,
                     tentative: Tentative = None, ignore_session_ids=(), venue_kind=None):
    """在指定日期与时间窗内为一组人找 (时段, 师傅, 材料)。返回 (placement|None, reasons)。"""
    kind = venue_kind or course["venue_kind"]
    sql = (
        "SELECT s.*, v.kind AS venue_kind, v.name AS venue_name FROM slots s "
        "JOIN venues v ON v.id = s.venue_id "
        "WHERE s.date=? AND v.status='active' "
        "AND s.start_min >= ? AND s.end_min <= ? AND (s.end_min - s.start_min) >= ? "
    )
    args = [date, win_start, win_end, course["duration_min"]]
    if kind != "any":
        sql += "AND v.kind = ? "
        args.append(kind)
    sql += "ORDER BY s.start_min"
    rows = conn.execute(sql, args).fetchall()
    reasons = []
    for slot in rows:
        free = slot["capacity"] - slot_used_capacity(conn, slot["id"], ignore_session_ids)
        if tentative:
            free -= tentative.slot_used.get(slot["id"], 0)
        if free < headcount:
            reasons.append(reason(
                "SLOT_CAPACITY_SHORT",
                f"{slot['venue_name']} {hhmm(slot['start_min'])}-{hhmm(slot['end_min'])} 剩余容量 {free} 人，装不下本组 {headcount} 人",
                {"slot_id": slot["id"], "venue_id": slot["venue_id"], "venue_name": slot["venue_name"],
                 "free": free, "need": headcount},
            ))
            continue
        masters, m_reasons = find_masters(conn, course["skill_reqs"], date, slot["start_min"],
                                          slot["end_min"], tentative, ignore_session_ids)
        if masters is None:
            reasons.extend(m_reasons)
            continue
        materials, mat_reasons = check_materials(conn, course["material_reqs"], headcount,
                                                 tentative, ignore_session_ids)
        if materials is None:
            reasons.extend(mat_reasons)
            continue
        return {
            "slot": {"id": slot["id"], "venue_id": slot["venue_id"], "venue_name": slot["venue_name"],
                     "venue_kind": slot["venue_kind"], "capacity": slot["capacity"]},
            "date": date, "start_min": slot["start_min"],
            "end_min": slot["start_min"] + course["duration_min"],
            "headcount": headcount, "masters": masters, "materials": materials,
        }, []
    if not rows:
        reasons.append(reason(
            "NO_SLOT",
            f"{date} {hhmm(win_start)}-{hhmm(win_end)} 没有符合课程时长（{course['duration_min']} 分钟）"
            f"与场地类型（{kind}）的时段",
            {"date": date, "window": f"{hhmm(win_start)}-{hhmm(win_end)}",
             "duration_min": course["duration_min"], "venue_kind": kind},
        ))
    return None, reasons
