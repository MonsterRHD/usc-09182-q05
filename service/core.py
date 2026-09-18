"""核心服务层：所有业务操作在此完成状态变更、不变量校验并追加事件日志。

关键不变量：
1. 已开始的体验（in_progress / locked=1）的资源不可被后续排程、请假、短缺、转室夺走；
2. 每次确认必须保存参与名单的最小信息（人数、年龄段、渠道侧摘要），不存个人身份；
3. 渠道重试（channel+external_ref）与离线补录（Idempotency-Key / 名单 idem_key）幂等；
4. 所有状态变化写事件日志，复盘可回放容量、改派与结算依据。
"""

from . import domain
from .errors import ApiError, bad_request, conflict, not_found  # ApiError 供调用方统一捕获
from .util import (add_days, check_date, check_hhmm, digest_of, dumps, hhmm, loads,
                   new_id, now_iso, parse_iso, session_end_iso, session_start_iso, today_iso)

DAY_START, DAY_END = 8 * 60, 18 * 60  # 全场可排程窗口 08:00-18:00
ALT_DATE_LOOKAHEAD = 3                # 替代方案最多向后找 3 天

# ---------------------------------------------------------------- 主数据维护


def create_course(conn, body):
    for f in ("name", "version", "duration_min", "group_size_max"):
        if body.get(f) is None:
            raise bad_request("MISSING_FIELD", f"缺少字段 {f}", {"field": f})
    if body["duration_min"] <= 0 or body["group_size_max"] <= 0:
        raise bad_request("INVALID_FIELD", "duration_min 与 group_size_max 必须为正数")
    kind = body.get("venue_kind", "any")
    if kind not in ("indoor", "outdoor", "any"):
        raise bad_request("INVALID_FIELD", "venue_kind 只能是 indoor/outdoor/any")
    days = int(body.get("days", 1))
    if days < 1:
        raise bad_request("INVALID_FIELD", "days 必须 >= 1")
    cid = body.get("id") or new_id("course")
    conn.execute(
        "INSERT INTO courses(id,name,version,duration_min,days,venue_kind,group_size_max,"
        "skill_reqs,material_reqs,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (cid, body["name"], str(body["version"]), int(body["duration_min"]), days, kind,
         int(body["group_size_max"]), dumps(body.get("skill_reqs", {})),
         dumps(body.get("material_reqs", {})), "active", now_iso()))
    return get_course(conn, cid)


def get_course(conn, cid):
    row = conn.execute("SELECT * FROM courses WHERE id=?", (cid,)).fetchone()
    if not row:
        raise not_found("COURSE_NOT_FOUND", f"课程版本 {cid} 不存在")
    return _course_dict(row)


def _course_dict(row):
    return {"id": row["id"], "name": row["name"], "version": row["version"],
            "duration_min": row["duration_min"], "days": row["days"],
            "venue_kind": row["venue_kind"], "group_size_max": row["group_size_max"],
            "skill_reqs": loads(row["skill_reqs"]), "material_reqs": loads(row["material_reqs"]),
            "status": row["status"], "created_at": row["created_at"]}


def list_courses(conn):
    return [_course_dict(r) for r in conn.execute("SELECT * FROM courses ORDER BY id")]


def create_venue(conn, body):
    for f in ("name", "kind"):
        if body.get(f) is None:
            raise bad_request("MISSING_FIELD", f"缺少字段 {f}", {"field": f})
    if body["kind"] not in ("indoor", "outdoor"):
        raise bad_request("INVALID_FIELD", "场地 kind 只能是 indoor/outdoor")
    vid = body.get("id") or new_id("venue")
    conn.execute("INSERT INTO venues(id,name,kind,status,created_at) VALUES(?,?,?,?,?)",
                 (vid, body["name"], body["kind"], "active", now_iso()))
    return _venue_dict(conn.execute("SELECT * FROM venues WHERE id=?", (vid,)).fetchone())


def _venue_dict(row):
    return {"id": row["id"], "name": row["name"], "kind": row["kind"],
            "status": row["status"], "created_at": row["created_at"]}


def list_venues(conn):
    return [_venue_dict(r) for r in conn.execute("SELECT * FROM venues ORDER BY id")]


def create_slot(conn, body):
    for f in ("venue_id", "date", "start", "end", "capacity"):
        if body.get(f) is None:
            raise bad_request("MISSING_FIELD", f"缺少字段 {f}", {"field": f})
    venue = conn.execute("SELECT * FROM venues WHERE id=?", (body["venue_id"],)).fetchone()
    if not venue:
        raise not_found("VENUE_NOT_FOUND", f"场地 {body['venue_id']} 不存在")
    date = check_date(body["date"])
    start, end = check_hhmm(body["start"], "start"), check_hhmm(body["end"], "end")
    if end <= start:
        raise bad_request("INVALID_SLOT", "时段结束必须晚于开始")
    if int(body["capacity"]) <= 0:
        raise bad_request("INVALID_SLOT", "时段容量必须为正数")
    sid = body.get("id") or new_id("slot")
    try:
        conn.execute(
            "INSERT INTO slots(id,venue_id,date,start_min,end_min,capacity) VALUES(?,?,?,?,?,?)",
            (sid, body["venue_id"], date, start, end, int(body["capacity"])))
    except Exception as e:
        if "UNIQUE" in str(e):
            raise conflict("SLOT_EXISTS", "该场地相同时间的时段已存在",
                           {"venue_id": body["venue_id"], "date": date,
                            "start": hhmm(start), "end": hhmm(end)})
        raise
    return _slot_dict(conn.execute("SELECT * FROM slots WHERE id=?", (sid,)).fetchone())


def _slot_dict(row, used=None):
    d = {"id": row["id"], "venue_id": row["venue_id"], "date": row["date"],
         "start": hhmm(row["start_min"]), "end": hhmm(row["end_min"]),
         "capacity": row["capacity"]}
    if used is not None:
        d["used"] = used
        d["free"] = row["capacity"] - used
    return d


def list_slots(conn, date=None, venue_id=None):
    sql, args = "SELECT * FROM slots WHERE 1=1", []
    if date:
        sql += " AND date=?"; args.append(check_date(date))
    if venue_id:
        sql += " AND venue_id=?"; args.append(venue_id)
    sql += " ORDER BY date, start_min"
    out = []
    for r in conn.execute(sql, args):
        out.append(_slot_dict(r, domain.slot_used_capacity(conn, r["id"])))
    return out


def create_master(conn, body):
    if not body.get("name"):
        raise bad_request("MISSING_FIELD", "缺少字段 name", {"field": "name"})
    skills = body.get("skills", [])
    if not isinstance(skills, list):
        raise bad_request("INVALID_FIELD", "skills 必须是数组")
    mid = body.get("id") or new_id("master")
    conn.execute("INSERT INTO masters(id,name,skills,status,created_at) VALUES(?,?,?,?,?)",
                 (mid, body["name"], dumps(skills), "active", now_iso()))
    return _master_dict(conn.execute("SELECT * FROM masters WHERE id=?", (mid,)).fetchone())


def _master_dict(row):
    return {"id": row["id"], "name": row["name"], "skills": loads(row["skills"]),
            "status": row["status"], "created_at": row["created_at"]}


def list_masters(conn):
    return [_master_dict(r) for r in conn.execute("SELECT * FROM masters ORDER BY id")]


def upsert_material(conn, body):
    if not body.get("name"):
        raise bad_request("MISSING_FIELD", "缺少字段 name", {"field": "name"})
    mid = body.get("id") or new_id("mat")
    stock = int(body.get("stock", 0))
    if stock < 0:
        raise bad_request("INVALID_FIELD", "库存不能为负")
    conn.execute(
        "INSERT INTO materials(id,name,unit,stock,updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, unit=excluded.unit, "
        "stock=excluded.stock, updated_at=excluded.updated_at",
        (mid, body["name"], body.get("unit", "套"), stock, now_iso()))
    return _material_dict(conn.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone(), conn)


def _material_dict(row, conn=None):
    d = {"id": row["id"], "name": row["name"], "unit": row["unit"],
         "stock": row["stock"], "updated_at": row["updated_at"]}
    if conn is not None:
        d["reserved"] = domain.reserved_material(conn, row["id"])
        d["available"] = row["stock"] - d["reserved"]
    return d


def list_materials(conn):
    return [_material_dict(r, conn) for r in conn.execute("SELECT * FROM materials ORDER BY id")]


# ---------------------------------------------------------------- 事件与状态


def _emit(conn, etype, entity_type, entity_id, payload=None, explanation=None, ts=None,
          biz_date=None):
    """追加事件。biz_date 是事件影响的业务日期（如会话日期），复盘按它回放；
    缺省取发生时间当天。"""
    ts = ts or now_iso()
    conn.execute(
        "INSERT INTO events(ts,biz_date,type,entity_type,entity_id,payload,explanation) "
        "VALUES(?,?,?,?,?,?,?)",
        (ts, biz_date or ts[:10], etype, entity_type, entity_id, dumps(payload or {}),
         explanation))


def _add_reason(existing: list, item: dict) -> list:
    return [r for r in existing if r.get("code") != item["code"]] + [item]


def _set_session_reasons(conn, session_id, reasons):
    conn.execute("UPDATE sessions SET state_reasons=?, updated_at=? WHERE id=?",
                 (dumps(reasons), now_iso(), session_id))


def _session_reasons(row):
    return loads(row["state_reasons"]) or []


# ---------------------------------------------------------------- 团队需求与方案


def create_booking(conn, body, occurred_at=None):
    """接收团队需求。渠道重试以 (channel, external_ref) 判重，返回已有单据。"""
    for f in ("channel", "external_ref", "org_name", "headcount", "course_id",
              "desired_date", "window_start", "window_end"):
        if body.get(f) is None:
            raise bad_request("MISSING_FIELD", f"缺少字段 {f}", {"field": f})
    channel, ref = str(body["channel"]), str(body["external_ref"])
    existing = conn.execute(
        "SELECT id FROM bookings WHERE channel=? AND external_ref=?", (channel, ref)).fetchone()
    if existing:
        return get_booking(conn, existing["id"]), True  # 幂等重放

    if int(body["headcount"]) <= 0:
        raise bad_request("INVALID_FIELD", "headcount 必须为正数")
    course = get_course(conn, body["course_id"])
    if course["status"] != "active":
        raise conflict("COURSE_INACTIVE", f"课程版本 {course['id']} 已停用")
    desired = check_date(body["desired_date"], "desired_date")
    ws, we = check_hhmm(body["window_start"], "window_start"), check_hhmm(body["window_end"], "window_end")
    if we <= ws:
        raise bad_request("INVALID_WINDOW", "期望时间窗结束必须晚于开始")

    bid = new_id("bk")
    ts = occurred_at or now_iso()
    conn.execute(
        "INSERT INTO bookings(id,channel,external_ref,org_name,headcount,course_id,desired_date,"
        "window_start,window_end,status,state_reasons,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (bid, channel, ref, body["org_name"], int(body["headcount"]), course["id"], desired,
         ws, we, "received", "[]", ts, ts))
    _emit(conn, "BOOKING_RECEIVED", "booking", bid,
          {"channel": channel, "external_ref": ref, "org_name": body["org_name"],
           "headcount": body["headcount"], "course_id": course["id"], "desired_date": desired,
           "window": f"{hhmm(ws)}-{hhmm(we)}"}, ts=ts, biz_date=desired)
    _generate_plans(conn, bid, course)
    return get_booking(conn, bid), False


def _generate_plans(conn, booking_id, course):
    bk = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    headcount, desired = bk["headcount"], bk["desired_date"]
    group_sizes = domain.split_groups(headcount, course["group_size_max"])

    attempts = [("primary", desired, bk["window_start"], bk["window_end"], [])]
    for delta in range(0, ALT_DATE_LOOKAHEAD + 1):
        d = add_days(desired, delta)
        if delta == 0:
            attempts.append(("alternative", d, DAY_START, DAY_END,
                             [f"时间窗放宽为全天 {hhmm(DAY_START)}-{hhmm(DAY_END)}"]))
        else:
            attempts.append(("alternative", d, DAY_START, DAY_END,
                             [f"日期 {desired} → {d}，时间窗放宽为全天"]))
    seen, made = set(), 0
    primary_reasons = []
    for kind, date, ws, we, changes in attempts:
        key = (date, ws, we)
        if key in seen:
            continue
        seen.add(key)
        groups, reasons = _try_place(conn, course, date, ws, we, group_sizes)
        if groups is None:
            if kind == "primary":
                primary_reasons = reasons
            continue
        made += 1
        _save_plan(conn, booking_id, kind, groups, changes, headcount, group_sizes,
                   date)
        if made >= 3:  # 最多保留 1 个首选 + 2 个替代
            break
    if made == 0:
        conn.execute("UPDATE bookings SET status='needs_attention', state_reasons=?, updated_at=? WHERE id=?",
                     (dumps(primary_reasons), now_iso(), booking_id))
        _emit(conn, "BOOKING_NEEDS_ATTENTION", "booking", booking_id,
              {"reasons": primary_reasons}, "首选与替代日期均无可行方案，需人工介入",
              biz_date=bk["desired_date"])
    else:
        conn.execute("UPDATE bookings SET status='planned', updated_at=? WHERE id=?",
                     (now_iso(), booking_id))


def _try_place(conn, course, date, ws, we, group_sizes):
    """尝试把每一天、每一组都放进同一日期窗口；跨日课程逐日放置。返回 (groups|None, reasons)。"""
    tentative = domain.Tentative()
    placements, all_reasons = [], []
    for day_index in range(course["days"]):
        d = add_days(date, day_index)
        for gi, size in enumerate(group_sizes):
            p, reasons = domain.search_placement(conn, course, d, ws, we, size, tentative)
            if p is None:
                return None, reasons
            p["group_no"], p["day_index"] = gi + 1, day_index
            tentative.add_placement(p)
            placements.append(p)
    return placements, all_reasons


def _save_plan(conn, booking_id, kind, groups, changes, headcount, group_sizes,
               biz_date):
    pid = new_id("plan")
    explanation = []
    if changes:
        explanation.append(domain.reason("ALTERNATIVE_ADJUSTMENT",
                                         "；".join(changes), {"changes": changes}))
    if len(group_sizes) > 1:
        explanation.append(domain.reason(
            "SPLIT_GROUP",
            f"{headcount} 人超出单组上限，拆为 {len(group_sizes)} 组：{'、'.join(map(str, group_sizes))} 人",
            {"group_sizes": group_sizes}))
    conn.execute("INSERT INTO plans(id,booking_id,kind,status,explanation,created_at) "
                 "VALUES(?,?,?,?,?,?)",
                 (pid, booking_id, kind, "proposed", dumps(explanation), now_iso()))
    for g in groups:
        conn.execute(
            "INSERT INTO plan_groups(id,plan_id,group_no,day_index,headcount,slot_id,venue_id,"
            "date,start_min,end_min,masters,materials) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id("pg"), pid, g["group_no"], g["day_index"], g["headcount"], g["slot"]["id"],
             g["slot"]["venue_id"], g["date"], g["start_min"], g["end_min"],
             dumps(g["masters"]), dumps(g["materials"])))
    _emit(conn, "PLAN_PROPOSED", "plan", pid,
          {"booking_id": booking_id, "kind": kind, "groups": len(groups)},
          explanation[0]["message"] if explanation else None, biz_date=biz_date)


def get_booking(conn, booking_id):
    bk = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not bk:
        raise not_found("BOOKING_NOT_FOUND", f"团队需求 {booking_id} 不存在")
    plans = []
    for p in conn.execute("SELECT * FROM plans WHERE booking_id=? ORDER BY created_at, id",
                          (booking_id,)):
        groups = [loads2(g) for g in conn.execute(
            "SELECT * FROM plan_groups WHERE plan_id=? ORDER BY day_index, group_no", (p["id"],))]
        plans.append({"id": p["id"], "kind": p["kind"], "status": p["status"],
                      "explanation": loads(p["explanation"]), "groups": groups})
    roster_rows = conn.execute("SELECT * FROM rosters WHERE booking_id=?", (booking_id,)).fetchall()
    return {"id": bk["id"], "channel": bk["channel"], "external_ref": bk["external_ref"],
            "org_name": bk["org_name"], "headcount": bk["headcount"],
            "course_id": bk["course_id"], "desired_date": bk["desired_date"],
            "window": f"{hhmm(bk['window_start'])}-{hhmm(bk['window_end'])}",
            "status": bk["status"], "state_reasons": loads(bk["state_reasons"]),
            "plans": plans,
            "rosters": [{"headcount": r["headcount"], "age_band": r["age_band"],
                         "roster_ref": r["roster_ref"], "digest": r["digest"],
                         "created_at": r["created_at"]} for r in roster_rows],
            "created_at": bk["created_at"], "updated_at": bk["updated_at"]}


def loads2(row):
    """plan_groups 行 → dict。"""
    return {"group_no": row["group_no"], "day_index": row["day_index"],
            "headcount": row["headcount"], "slot_id": row["slot_id"],
            "venue_id": row["venue_id"], "date": row["date"],
            "start": hhmm(row["start_min"]), "end": hhmm(row["end_min"]),
            "masters": loads(row["masters"]), "materials": loads(row["materials"])}


def list_bookings(conn, date=None, status=None):
    sql, args = "SELECT id FROM bookings WHERE 1=1", []
    if date:
        sql += " AND desired_date=?"; args.append(check_date(date, "date"))
    if status:
        sql += " AND status=?"; args.append(status)
    sql += " ORDER BY created_at"
    return [get_booking(conn, r["id"]) for r in conn.execute(sql, args)]


# ---------------------------------------------------------------- 确认与名单


def confirm_booking(conn, booking_id, body):
    """确认方案：保存名单最小信息，落库分组会话并锁定容量/师傅/材料。"""
    bk = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not bk:
        raise not_found("BOOKING_NOT_FOUND", f"团队需求 {booking_id} 不存在")
    if bk["status"] == "confirmed":
        # 幂等：重复确认返回当前状态
        return get_booking(conn, booking_id)
    if bk["status"] not in ("planned",):
        raise conflict("BOOKING_NOT_PLANNABLE",
                       f"当前状态 {bk['status']} 不可确认",
                       {"status": bk["status"], "state_reasons": loads(bk["state_reasons"])})
    plan_id = body.get("plan_id")
    if plan_id:
        plan = conn.execute("SELECT * FROM plans WHERE id=? AND booking_id=?",
                            (plan_id, booking_id)).fetchone()
        if plan is None:
            raise not_found("PLAN_NOT_FOUND", f"方案 {plan_id} 不存在或不属于该需求",
                            {"plan_id": plan_id})
    else:
        plan = conn.execute("SELECT * FROM plans WHERE booking_id=? AND status='proposed' "
                            "ORDER BY CASE kind WHEN 'primary' THEN 0 ELSE 1 END, created_at "
                            "LIMIT 1", (booking_id,)).fetchone()
    if plan is None or plan["status"] != "proposed":
        raise conflict("PLAN_NOT_AVAILABLE", "没有可确认的方案", {"plan_id": plan_id})

    roster = body.get("roster")
    if not roster or roster.get("headcount") is None:
        raise bad_request("ROSTER_REQUIRED",
                          "确认时必须保存参与名单最小信息（headcount，可选 age_band / roster_ref）")
    if int(roster["headcount"]) != bk["headcount"]:
        raise bad_request("ROSTER_MISMATCH",
                          f"名单人数 {roster['headcount']} 与需求人数 {bk['headcount']} 不一致")
    idem_key = str(roster.get("idem_key") or body.get("idem_key") or "confirm")
    minimal = {"headcount": int(roster["headcount"])}
    if roster.get("age_band"):
        minimal["age_band"] = str(roster["age_band"])
    if roster.get("roster_ref"):
        minimal["roster_ref"] = str(roster["roster_ref"])
    digest = digest_of({"booking_id": booking_id, **minimal})

    groups = conn.execute("SELECT * FROM plan_groups WHERE plan_id=? ORDER BY day_index, group_no",
                          (plan["id"],)).fetchall()
    course = _course_dict(conn.execute("SELECT * FROM courses WHERE id=?",
                                       (bk["course_id"],)).fetchone())
    # 确认时按当前库状态复核所有资源；失败则整体回滚并给出解释
    problems = []
    for g in groups:
        slot = conn.execute("SELECT * FROM slots WHERE id=?", (g["slot_id"],)).fetchone()
        free = slot["capacity"] - domain.slot_used_capacity(conn, slot["id"])
        if free < g["headcount"]:
            problems.append(domain.reason(
                "SLOT_CAPACITY_SHORT",
                f"{g['date']} {hhmm(g['start_min'])}-{hhmm(g['end_min'])} 剩余容量 {free} 人，"
                f"装不下第 {g['group_no']} 组 {g['headcount']} 人",
                {"slot_id": slot["id"], "free": free, "need": g["headcount"]}))
        masters = loads(g["masters"])
        for m in masters:
            busy = domain._master_busy_session_ids(conn, m["master_id"], g["date"],
                                                   g["start_min"], g["end_min"])
            if busy or domain._master_on_leave(conn, m["master_id"],
                                               session_start_iso(g["date"], g["start_min"]),
                                               session_end_iso(g["date"], g["end_min"])):
                problems.append(domain.reason(
                    "MASTER_UNAVAILABLE",
                    f"师傅 {m['master_name']} 在 {g['date']} {hhmm(g['start_min'])}-"
                    f"{hhmm(g['end_min'])} 已不可用",
                    {"master_id": m["master_id"], "date": g["date"]}))
        for mid, qty in loads(g["materials"]).items():
            available = conn.execute("SELECT stock FROM materials WHERE id=?",
                                     (mid,)).fetchone()["stock"] - domain.reserved_material(conn, mid)
            if qty > available:
                problems.append(domain.reason(
                    "MATERIAL_SHORTAGE", f"材料 {mid} 可用 {available}，本组需要 {qty}",
                    {"material_id": mid, "need": qty, "available": available}))
    if problems:
        raise conflict("PLAN_STALE", "方案生成后资源已变化，请重新规划", {"reasons": problems})

    ts = now_iso()
    for g in groups:
        sid = new_id("ss")
        conn.execute(
            "INSERT INTO sessions(id,booking_id,plan_id,group_no,day_index,course_id,venue_id,"
            "slot_id,date,start_min,end_min,headcount,status,locked,state_reasons,created_at,"
            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, booking_id, plan["id"], g["group_no"], g["day_index"], bk["course_id"],
             g["venue_id"], g["slot_id"], g["date"], g["start_min"], g["end_min"],
             g["headcount"], "confirmed", 0, "[]", ts, ts))
        for m in loads(g["masters"]):
            conn.execute(
                "INSERT INTO session_masters(id,session_id,master_id,skill,status,assigned_at) "
                "VALUES(?,?,?,?,?,?)",
                (new_id("sm"), sid, m["master_id"], m["skill"], "assigned", ts))
        for mid, qty in loads(g["materials"]).items():
            conn.execute(
                "INSERT INTO session_materials(session_id,material_id,qty_reserved,status) "
                "VALUES(?,?,?,?)", (sid, mid, qty, "reserved"))
    conn.execute("INSERT INTO rosters(booking_id,idem_key,headcount,age_band,roster_ref,digest,"
                 "created_at) VALUES(?,?,?,?,?,?,?) "
                 "ON CONFLICT(booking_id,idem_key) DO NOTHING",
                 (booking_id, idem_key, minimal["headcount"], minimal.get("age_band"),
                  minimal.get("roster_ref"), digest, ts))
    conn.execute("UPDATE bookings SET status='confirmed', updated_at=? WHERE id=?", (ts, booking_id))
    conn.execute("UPDATE plans SET status='confirmed' WHERE id=?", (plan["id"],))
    conn.execute("UPDATE plans SET status='superseded' WHERE booking_id=? AND id != ? "
                 "AND status='proposed'", (booking_id, plan["id"]))
    _emit(conn, "BOOKING_CONFIRMED", "booking", booking_id,
          {"plan_id": plan["id"], "sessions": len(groups)}, ts=ts,
          biz_date=bk["desired_date"])
    _emit(conn, "ROSTER_SAVED", "booking", booking_id,
          {"minimal": minimal, "digest": digest},
          "仅保存名单最小信息（人数/年龄段/渠道摘要哈希），不存个人身份", ts=ts,
          biz_date=bk["desired_date"])
    return get_booking(conn, booking_id)


def save_roster(conn, booking_id, body, occurred_at=None):
    """离线补录名单：以 (booking_id, idem_key) 幂等；只保存最小信息。"""
    bk = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not bk:
        raise not_found("BOOKING_NOT_FOUND", f"团队需求 {booking_id} 不存在")
    for f in ("idem_key", "headcount"):
        if body.get(f) is None:
            raise bad_request("MISSING_FIELD", f"缺少字段 {f}", {"field": f})
    if int(body["headcount"]) != bk["headcount"]:
        raise bad_request("ROSTER_MISMATCH",
                          f"名单人数 {body['headcount']} 与需求人数 {bk['headcount']} 不一致")
    minimal = {"headcount": int(body["headcount"])}
    if body.get("age_band"):
        minimal["age_band"] = str(body["age_band"])
    if body.get("roster_ref"):
        minimal["roster_ref"] = str(body["roster_ref"])
    digest = digest_of({"booking_id": booking_id, **minimal})
    ts = occurred_at or now_iso()
    cur = conn.execute(
        "INSERT INTO rosters(booking_id,idem_key,headcount,age_band,roster_ref,digest,created_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(booking_id,idem_key) DO NOTHING",
        (booking_id, str(body["idem_key"]), minimal["headcount"], minimal.get("age_band"),
         minimal.get("roster_ref"), digest, ts))
    if cur.rowcount:
        _emit(conn, "ROSTER_SAVED", "booking", booking_id,
              {"minimal": minimal, "digest": digest, "offline": True},
              "离线补录名单，仅保存最小信息", ts=ts, biz_date=bk["desired_date"])
    return get_booking(conn, booking_id)


def cancel_booking(conn, booking_id, body):
    bk = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not bk:
        raise not_found("BOOKING_NOT_FOUND", f"团队需求 {booking_id} 不存在")
    if bk["status"] in ("cancelled", "completed"):
        return get_booking(conn, booking_id)  # 幂等
    sessions = conn.execute("SELECT * FROM sessions WHERE booking_id=?", (booking_id,)).fetchall()
    started = [s for s in sessions if s["status"] == "in_progress"]
    if started:
        raise conflict(
            "SESSION_LOCKED", "已开始的体验不可取消，其资源受保护",
            {"locked_sessions": [s["id"] for s in started]})
    ts = now_iso()
    for s in sessions:
        if s["status"] == "confirmed":
            _release_session(conn, s, ts)
    new_status = "cancelled"
    if any(s["status"] == "completed" for s in sessions):
        new_status = "partially_cancelled"
    reason_text = (body or {}).get("reason") or "未说明原因"
    reasons = _add_reason(loads(bk["state_reasons"]) or [], domain.reason(
        "CANCELLED", f"团队取消：{reason_text}；已释放未开始会话的场地/师傅/材料",
        {"reason": reason_text,
         "released_sessions": [s["id"] for s in sessions if s["status"] == "confirmed"]}))
    conn.execute("UPDATE bookings SET status=?, state_reasons=?, updated_at=? WHERE id=?",
                 (new_status, dumps(reasons), ts, booking_id))
    _emit(conn, "BOOKING_CANCELLED", "booking", booking_id,
          {"reason": reason_text,
           "released_sessions": [s["id"] for s in sessions if s["status"] == "confirmed"]},
          reason_text, ts=ts, biz_date=bk["desired_date"])
    return get_booking(conn, booking_id)


def _release_session(conn, s, ts):
    conn.execute("UPDATE sessions SET status='cancelled', updated_at=? WHERE id=?", (ts, s["id"]))
    conn.execute("UPDATE session_masters SET status='released', released_at=? "
                 "WHERE session_id=? AND status='assigned'", (ts, s["id"]))
    conn.execute("UPDATE session_materials SET status='released' WHERE session_id=? "
                 "AND status='reserved'", (s["id"],))
    _emit(conn, "SESSION_CANCELLED", "session", s["id"],
          {"booking_id": s["booking_id"]}, ts=ts, biz_date=s["date"])


# ---------------------------------------------------------------- 会话执行


def _get_session(conn, session_id):
    s = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not s:
        raise not_found("SESSION_NOT_FOUND", f"体验会话 {session_id} 不存在")
    return s


def get_session(conn, session_id):
    s = _get_session(conn, session_id)
    masters = conn.execute(
        "SELECT sm.master_id, m.name AS master_name, sm.skill, sm.status, sm.assigned_at, "
        "sm.released_at FROM session_masters sm JOIN masters m ON m.id=sm.master_id "
        "WHERE sm.session_id=? ORDER BY sm.assigned_at", (session_id,)).fetchall()
    materials = conn.execute(
        "SELECT sm.material_id, mt.name, mt.unit, sm.qty_reserved, sm.qty_consumed, sm.status "
        "FROM session_materials sm JOIN materials mt ON mt.id=sm.material_id "
        "WHERE sm.session_id=?", (session_id,)).fetchall()
    venue = conn.execute("SELECT name, kind FROM venues WHERE id=?", (s["venue_id"],)).fetchone()
    return {"id": s["id"], "booking_id": s["booking_id"], "plan_id": s["plan_id"],
            "group_no": s["group_no"], "day_index": s["day_index"],
            "course_id": s["course_id"], "venue_id": s["venue_id"],
            "venue_name": venue["name"], "venue_kind": venue["kind"],
            "slot_id": s["slot_id"], "date": s["date"],
            "start": hhmm(s["start_min"]), "end": hhmm(s["end_min"]),
            "headcount": s["headcount"], "attended": s["attended"],
            "status": s["status"], "locked": bool(s["locked"]),
            "state_reasons": loads(s["state_reasons"]),
            "masters": [dict(m) for m in masters],
            "materials": [dict(m) for m in materials],
            "created_at": s["created_at"], "updated_at": s["updated_at"]}


def list_sessions(conn, date=None, status=None, booking_id=None):
    sql, args = "SELECT id FROM sessions WHERE 1=1", []
    if date:
        sql += " AND date=?"; args.append(check_date(date, "date"))
    if status:
        sql += " AND status=?"; args.append(status)
    if booking_id:
        sql += " AND booking_id=?"; args.append(booking_id)
    sql += " ORDER BY date, start_min, id"
    return [get_session(conn, r["id"]) for r in conn.execute(sql, args)]


def start_session(conn, session_id):
    s = _get_session(conn, session_id)
    if s["status"] == "in_progress":
        return get_session(conn, session_id)  # 幂等
    if s["status"] != "confirmed":
        raise conflict("SESSION_NOT_STARTABLE", f"状态 {s['status']} 不可开始",
                       {"status": s["status"]})
    ts = now_iso()
    conn.execute("UPDATE sessions SET status='in_progress', locked=1, updated_at=? WHERE id=?",
                 (ts, session_id))
    reasons = _add_reason(_session_reasons(s), domain.reason(
        "RESOURCE_LOCKED", "体验已开始，场地/师傅/材料已锁定，后续排程不得夺走",
        {"locked_at": ts}))
    _set_session_reasons(conn, session_id, reasons)
    _emit(conn, "SESSION_STARTED", "session", session_id,
          {"booking_id": s["booking_id"], "date": s["date"]}, "资源已锁定", ts=ts,
          biz_date=s["date"])
    return get_session(conn, session_id)


def complete_session(conn, session_id, body):
    s = _get_session(conn, session_id)
    if s["status"] == "completed":
        return get_session(conn, session_id)  # 幂等
    if s["status"] != "in_progress":
        raise conflict("SESSION_NOT_COMPLETABLE", f"状态 {s['status']} 不可完结",
                       {"status": s["status"]})
    attended = (body or {}).get("attended", s["headcount"])
    attended = int(attended)
    if attended < 0 or attended > s["headcount"]:
        raise bad_request("INVALID_ATTENDED", "实到人数必须在 0 与计划人数之间")
    ts = now_iso()
    course = _course_dict(conn.execute("SELECT * FROM courses WHERE id=?",
                                       (s["course_id"],)).fetchone())
    consumed = {}
    for mid, per in course["material_reqs"].items():
        qty = per * attended
        conn.execute("UPDATE session_materials SET status='consumed', qty_consumed=? "
                     "WHERE session_id=? AND material_id=?", (qty, session_id, mid))
        conn.execute("UPDATE materials SET stock=stock-?, updated_at=? WHERE id=?",
                     (qty, ts, mid))
        consumed[mid] = qty
    conn.execute("UPDATE sessions SET status='completed', attended=?, updated_at=? WHERE id=?",
                 (attended, ts, session_id))
    conn.execute("UPDATE session_masters SET status='released', released_at=? "
                 "WHERE session_id=? AND status='assigned'", (ts, session_id))
    _emit(conn, "SESSION_COMPLETED", "session", session_id,
          {"booking_id": s["booking_id"], "attended": attended, "materials_consumed": consumed},
          ts=ts, biz_date=s["date"])
    _maybe_complete_booking(conn, s["booking_id"], ts)
    return get_session(conn, session_id)


def _maybe_complete_booking(conn, booking_id, ts):
    rows = conn.execute("SELECT status FROM sessions WHERE booking_id=?", (booking_id,)).fetchall()
    if rows and all(r["status"] in ("completed", "cancelled") for r in rows):
        if any(r["status"] == "completed" for r in rows):
            conn.execute("UPDATE bookings SET status='completed', updated_at=? WHERE id=?",
                         (ts, booking_id))


# ---------------------------------------------------------------- 现场事件：迟到/拆团/转室


def report_late(conn, session_id, body):
    s = _get_session(conn, session_id)
    if s["status"] not in ("confirmed", "in_progress"):
        raise conflict("SESSION_NOT_ACTIVE", f"状态 {s['status']} 不可上报迟到",
                       {"status": s["status"]})
    minutes = int((body or {}).get("minutes", 0))
    if minutes <= 0:
        raise bad_request("INVALID_FIELD", "minutes 必须为正数")
    slot = conn.execute("SELECT * FROM slots WHERE id=?", (s["slot_id"],)).fetchone()
    slack = slot["end_min"] - s["end_min"]
    if minutes <= slack:
        impact, msg = "extend", f"迟到 {minutes} 分钟，时段内可顺延，课程时长不变"
    else:
        impact, msg = "compress", (f"迟到 {minutes} 分钟，超出时段余量 {slack} 分钟，"
                                   f"课程压缩 {minutes - slack} 分钟")
    item = domain.reason("LATE_ARRIVAL", msg,
                         {"minutes": minutes, "slot_slack": slack, "impact": impact})
    _set_session_reasons(conn, session_id, _add_reason(_session_reasons(s), item))
    _emit(conn, "LATE_ARRIVAL", "session", session_id, item["facts"], msg,
          biz_date=s["date"])
    return get_session(conn, session_id)


def split_session(conn, session_id, body):
    """拆团：原会话保留第一部分，新部分另找资源。已开始会话不可拆。"""
    s = _get_session(conn, session_id)
    if s["status"] == "in_progress":
        raise conflict("SESSION_LOCKED", "已开始的体验不可拆团，资源已锁定",
                       {"session_id": session_id})
    if s["status"] != "confirmed":
        raise conflict("SESSION_NOT_SPLITTABLE", f"状态 {s['status']} 不可拆团",
                       {"status": s["status"]})
    parts = (body or {}).get("parts")
    if not isinstance(parts, list) or len(parts) != 2 or any(int(p) <= 0 for p in parts):
        raise bad_request("INVALID_PARTS", "parts 必须是两个正整数，如 [20, 15]")
    keep, move = int(parts[0]), int(parts[1])
    if keep + move != s["headcount"]:
        raise bad_request("PARTS_MISMATCH",
                          f"拆分 {keep}+{move} 与原人数 {s['headcount']} 不一致")
    course = _course_dict(conn.execute("SELECT * FROM courses WHERE id=?",
                                       (s["course_id"],)).fetchone())
    if move > course["group_size_max"] or keep > course["group_size_max"]:
        raise bad_request("GROUP_TOO_LARGE",
                          f"拆分后每组不得超过课程上限 {course['group_size_max']} 人")
    # 先缩减原会话占用，再为分出部分寻找新资源（同一事务，失败整体回滚）。
    # 注意：原会话的人数与材料预留已按 keep 缩减落库，查找时直接反映拆后余量，
    # 原会话的师傅仍在带原组，新组必须另派师傅。
    ts = now_iso()
    conn.execute("UPDATE sessions SET headcount=?, updated_at=? WHERE id=?", (keep, ts, session_id))
    for mid, per in course["material_reqs"].items():
        conn.execute("UPDATE session_materials SET qty_reserved=? WHERE session_id=? "
                     "AND material_id=? AND status='reserved'", (per * keep, session_id, mid))
    placement, reasons = domain.search_placement(
        conn, course, s["date"], DAY_START, DAY_END, move)
    if placement is None:
        raise conflict("SPLIT_NOT_FEASIBLE", "拆出的部分找不到可用场地/师傅/材料",
                       {"reasons": reasons})
    new_sid = new_id("ss")
    max_group = conn.execute("SELECT MAX(group_no) AS g FROM sessions WHERE booking_id=?",
                             (s["booking_id"],)).fetchone()["g"]
    conn.execute(
        "INSERT INTO sessions(id,booking_id,plan_id,group_no,day_index,course_id,venue_id,"
        "slot_id,date,start_min,end_min,headcount,status,locked,state_reasons,created_at,"
        "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_sid, s["booking_id"], s["plan_id"], (max_group or 0) + 1, s["day_index"],
         s["course_id"], placement["slot"]["venue_id"], placement["slot"]["id"],
         placement["date"], placement["start_min"], placement["end_min"], move,
         "confirmed", 0, "[]", ts, ts))
    for m in placement["masters"]:
        conn.execute("INSERT INTO session_masters(id,session_id,master_id,skill,status,"
                     "assigned_at) VALUES(?,?,?,?,?,?)",
                     (new_id("sm"), new_sid, m["master_id"], m["skill"], "assigned", ts))
    for mid, qty in placement["materials"].items():
        conn.execute("INSERT INTO session_materials(session_id,material_id,qty_reserved,status) "
                     "VALUES(?,?,?,?)", (new_sid, mid, qty, "reserved"))
    item = domain.reason(
        "SPLIT_GROUP",
        f"拆团：{s['headcount']} 人拆为 {keep} 人（原组）与 {move} 人（新组，"
        f"{placement['date']} {hhmm(placement['start_min'])}-{hhmm(placement['end_min'])} "
        f"{placement['slot']['venue_name']}）",
        {"keep": keep, "move": move, "new_session_id": new_sid})
    _set_session_reasons(conn, session_id, _add_reason(_session_reasons(s), item))
    _emit(conn, "SESSION_SPLIT", "session", session_id,
          {**item["facts"], "booking_id": s["booking_id"]}, item["message"], ts=ts,
          biz_date=s["date"])
    return {"original": get_session(conn, session_id), "new_session": get_session(conn, new_sid)}


def transfer_session(conn, session_id, body):
    """雨天转室：把会话迁往同时段的室内场地。原场地类型不符或天气原因时触发。"""
    s = _get_session(conn, session_id)
    if s["status"] not in ("confirmed", "in_progress"):
        raise conflict("SESSION_NOT_ACTIVE", f"状态 {s['status']} 不可转室",
                       {"status": s["status"]})
    reason_text = (body or {}).get("reason", "rain")
    venue = conn.execute("SELECT * FROM venues WHERE id=?", (s["venue_id"],)).fetchone()
    if venue["kind"] == "indoor":
        raise conflict("ALREADY_INDOOR", "会话已在室内场地，无需转室",
                       {"venue_id": s["venue_id"]})
    rows = conn.execute(
        "SELECT s.*, v.name AS venue_name FROM slots s JOIN venues v ON v.id=s.venue_id "
        "WHERE s.date=? AND v.kind='indoor' AND v.status='active' "
        "AND s.start_min <= ? AND s.end_min >= ? ORDER BY s.capacity DESC",
        (s["date"], s["start_min"], s["end_min"])).fetchall()
    target = None
    for slot in rows:
        free = slot["capacity"] - domain.slot_used_capacity(conn, slot["id"])
        if free >= s["headcount"]:
            target = slot
            break
    if target is None:
        item = domain.reason(
            "TRANSFER_FAILED",
            f"雨天转室失败：{s['date']} {hhmm(s['start_min'])}-{hhmm(s['end_min'])} "
            f"没有可容纳 {s['headcount']} 人的室内场地",
            {"date": s["date"], "need": s["headcount"]})
        _set_session_reasons(conn, session_id, _add_reason(_session_reasons(s), item))
        _emit(conn, "TRANSFER_FAILED", "session", session_id, item["facts"],
              item["message"], biz_date=s["date"])
        raise conflict("NO_INDOOR_VENUE", item["message"], item["facts"])
    ts = now_iso()
    conn.execute("UPDATE sessions SET venue_id=?, slot_id=?, updated_at=? WHERE id=?",
                 (target["venue_id"], target["id"], ts, session_id))
    item = domain.reason(
        "MOVED_INDOOR_RAIN" if reason_text == "rain" else "TRANSFERRED",
        f"{'雨天转室' if reason_text == 'rain' else '转场'}：{venue['name']}（室外）→ "
        f"{target['venue_name']}（室内），时间不变",
        {"from_venue_id": s["venue_id"], "to_venue_id": target["venue_id"],
         "reason": reason_text})
    _set_session_reasons(conn, session_id, _add_reason(_session_reasons(s), item))
    _emit(conn, "SESSION_TRANSFERRED", "session", session_id,
          {**item["facts"], "booking_id": s["booking_id"]}, item["message"], ts=ts,
          biz_date=s["date"])
    return get_session(conn, session_id)


# ---------------------------------------------------------------- 材料短缺与师傅请假


def adjust_material(conn, material_id, body):
    """库存调整。调减可能引发短缺：已开始的会话优先保住，缺口落在最晚的未开始会话上。"""
    row = conn.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
    if not row:
        raise not_found("MATERIAL_NOT_FOUND", f"材料 {material_id} 不存在")
    delta = int((body or {}).get("delta", 0))
    reason_text = (body or {}).get("reason", "manual_adjust")
    new_stock = row["stock"] + delta
    if new_stock < 0:
        raise conflict("STOCK_NEGATIVE", f"调整后库存 {new_stock} 为负，请先释放预留",
                       {"stock": row["stock"], "delta": delta})
    ts = now_iso()
    conn.execute("UPDATE materials SET stock=?, updated_at=? WHERE id=?", (new_stock, ts, material_id))
    _emit(conn, "MATERIAL_ADJUSTED", "material", material_id,
          {"delta": delta, "stock": new_stock, "reason": reason_text}, ts=ts)
    affected = _recompute_material_coverage(conn, material_id, ts)
    return {"material": _material_dict(conn.execute(
        "SELECT * FROM materials WHERE id=?", (material_id,)).fetchone(), conn),
        "affected_sessions": affected}


def _recompute_material_coverage(conn, material_id, ts):
    """按「已开始锁定优先，其次按开始时间先后」分配库存，覆盖不到的标记短缺。"""
    stock = conn.execute("SELECT stock, name, unit FROM materials WHERE id=?",
                         (material_id,)).fetchone()
    rows = conn.execute(
        "SELECT s.id, s.date, s.start_min, s.locked, sm.qty_reserved, s.state_reasons "
        "FROM session_materials sm JOIN sessions s ON s.id=sm.session_id "
        "WHERE sm.material_id=? AND sm.status='reserved' AND s.status IN ('confirmed','in_progress') "
        "ORDER BY s.locked DESC, s.date, s.start_min, s.id", (material_id,)).fetchall()
    remaining = stock["stock"]
    affected = []
    for r in rows:
        reasons = [x for x in (loads(r["state_reasons"]) or [])
                   if not (x.get("code") == "MATERIAL_SHORTAGE"
                           and x.get("facts", {}).get("material_id") == material_id)]
        if r["qty_reserved"] <= remaining:
            remaining -= r["qty_reserved"]
            _set_session_reasons(conn, r["id"], reasons)  # 补足后自动解除短缺标记
            continue
        gap = r["qty_reserved"] - remaining
        remaining = 0
        item = domain.reason(
            "MATERIAL_SHORTAGE",
            f"材料「{stock['name']}」临时短缺：本组需要 {r['qty_reserved']}{stock['unit']}，"
            f"按锁定与先到先得分配后缺口 {gap}{stock['unit']}",
            {"material_id": material_id, "material_name": stock["name"],
             "need": r["qty_reserved"], "gap": gap, "unit": stock["unit"]})
        _set_session_reasons(conn, r["id"], _add_reason(reasons, item))
        _emit(conn, "MATERIAL_SHORTAGE", "session", r["id"], item["facts"],
              item["message"], ts=ts, biz_date=r["date"])
        affected.append({"session_id": r["id"], "gap": gap})
    return affected


def master_leave(conn, master_id, body):
    """师傅请假：自动改派同技能师傅；改派不了的给出可解释状态。
    与已开始会话重叠的请假被拒绝——已开始体验的资源不可被夺走。"""
    m = conn.execute("SELECT * FROM masters WHERE id=?", (master_id,)).fetchone()
    if not m:
        raise not_found("MASTER_NOT_FOUND", f"师傅 {master_id} 不存在")
    start_at = parse_iso((body or {}).get("start_at"), "start_at")
    end_at = parse_iso((body or {}).get("end_at"), "end_at")
    if end_at <= start_at:
        raise bad_request("INVALID_LEAVE", "请假结束必须晚于开始")
    rows = conn.execute(
        "SELECT sm.id AS sm_id, s.* FROM session_masters sm JOIN sessions s ON s.id=sm.session_id "
        "WHERE sm.master_id=? AND sm.status='assigned' AND s.status IN ('confirmed','in_progress')",
        (master_id,)).fetchall()
    locked_hit = [r for r in rows if r["status"] == "in_progress" and _overlap_session(
        r, start_at, end_at)]
    if locked_hit:
        raise conflict(
            "SESSION_LOCKED",
            "请假时段与已开始的体验重叠，该体验资源受保护，请改派其他师傅或调整请假时间",
            {"locked_sessions": [r["id"] for r in locked_hit]})
    affected = [r for r in rows if _overlap_session(r, start_at, end_at)]
    ts = now_iso()
    leave_id = new_id("leave")
    conn.execute("INSERT INTO master_leaves(id,master_id,start_at,end_at,reason,created_at) "
                 "VALUES(?,?,?,?,?,?)",
                 (leave_id, master_id, start_at, end_at, (body or {}).get("reason"), ts))
    _emit(conn, "MASTER_LEAVE", "master", master_id,
          {"leave_id": leave_id, "start_at": start_at, "end_at": end_at,
           "reason": (body or {}).get("reason")}, ts=ts, biz_date=start_at[:10])
    reassignments, unassigned = [], []
    for r in affected:
        sm = conn.execute("SELECT * FROM session_masters WHERE id=?", (r["sm_id"],)).fetchone()
        candidates, _ = domain.find_masters(
            conn, {sm["skill"]: 1}, r["date"], r["start_min"], r["end_min"],
            ignore_session_ids=(r["id"],))
        if candidates:
            new_m = candidates[0]
            conn.execute("UPDATE session_masters SET status='replaced', released_at=? WHERE id=?",
                         (ts, sm["id"]))
            conn.execute("INSERT INTO session_masters(id,session_id,master_id,skill,status,"
                         "assigned_at) VALUES(?,?,?,?,?,?)",
                         (new_id("sm"), r["id"], new_m["master_id"], sm["skill"], "assigned", ts))
            item = domain.reason(
                "MASTER_REASSIGNED",
                f"师傅 {m['name']} 请假，{r['date']} {hhmm(r['start_min'])}-"
                f"{hhmm(r['end_min'])} 的「{sm['skill']}」改派 {new_m['master_name']}",
                {"from_master_id": master_id, "to_master_id": new_m["master_id"],
                 "skill": sm["skill"]})
            _set_session_reasons(conn, r["id"], _add_reason(loads(r["state_reasons"]) or [], item))
            _emit(conn, "MASTER_REASSIGNED", "session", r["id"], item["facts"],
                  item["message"], ts=ts, biz_date=r["date"])
            reassignments.append({"session_id": r["id"], "to_master_id": new_m["master_id"],
                                  "to_master_name": new_m["master_name"], "skill": sm["skill"]})
        else:
            item = domain.reason(
                "MASTER_UNASSIGNED",
                f"师傅 {m['name']} 请假，{r['date']} {hhmm(r['start_min'])}-"
                f"{hhmm(r['end_min'])} 的「{sm['skill']}」暂无同技能师傅可改派，需人工协调",
                {"from_master_id": master_id, "skill": sm["skill"]})
            _set_session_reasons(conn, r["id"], _add_reason(loads(r["state_reasons"]) or [], item))
            _emit(conn, "MASTER_UNASSIGNED", "session", r["id"], item["facts"],
                  item["message"], ts=ts, biz_date=r["date"])
            unassigned.append({"session_id": r["id"], "skill": sm["skill"]})
    return {"leave_id": leave_id, "master_id": master_id,
            "reassignments": reassignments, "unassigned": unassigned}


def _overlap_session(session_row, start_at, end_at):
    s_iso = session_start_iso(session_row["date"], session_row["start_min"])
    e_iso = session_end_iso(session_row["date"], session_row["end_min"])
    return s_iso < end_at and start_at < e_iso


# ---------------------------------------------------------------- 交接 / 回放 / 结算


def handover(conn, date):
    """系统恢复后的交接视图：当天（默认今天）未结束的会话及其资源与待办状态。"""
    date = check_date(date, "date") if date else today_iso()
    sessions = list_sessions(conn, date=date)
    open_sessions = [s for s in sessions if s["status"] in ("confirmed", "in_progress")]
    return {"date": date, "open_sessions": open_sessions,
            "summary": {"total": len(sessions), "open": len(open_sessions),
                        "in_progress": sum(1 for s in open_sessions if s["status"] == "in_progress"),
                        "confirmed": sum(1 for s in open_sessions if s["status"] == "confirmed")}}


def replay(conn, from_date, to_date):
    """业务复盘回放：事件时间线 + 每日容量核对 + 改派记录 + 结算依据。"""
    frm, to = check_date(from_date, "from"), check_date(to_date, "to")
    events = [{"id": e["id"], "ts": e["ts"], "biz_date": e["biz_date"], "type": e["type"],
               "entity_type": e["entity_type"], "entity_id": e["entity_id"],
               "payload": loads(e["payload"]), "explanation": e["explanation"]}
              for e in conn.execute(
                  "SELECT * FROM events WHERE biz_date BETWEEN ? AND ? ORDER BY id",
                  (frm, to))]
    capacity = []
    for slot in conn.execute(
            "SELECT s.*, v.name AS venue_name FROM slots s JOIN venues v ON v.id=s.venue_id "
            "WHERE s.date BETWEEN ? AND ? ORDER BY s.date, s.start_min", (frm, to)):
        used = domain.slot_used_capacity(conn, slot["id"])
        capacity.append({"date": slot["date"], "slot_id": slot["id"],
                         "venue_name": slot["venue_name"],
                         "window": f"{hhmm(slot['start_min'])}-{hhmm(slot['end_min'])}",
                         "capacity": slot["capacity"], "used": used,
                         "free": slot["capacity"] - used,
                         "overbooked": used > slot["capacity"]})
    reassignments = [e for e in events if e["type"] in
                     ("MASTER_REASSIGNED", "MASTER_UNASSIGNED", "MASTER_LEAVE")]
    return {"from": frm, "to": to, "events": events, "capacity": capacity,
            "reassignments": reassignments, "settlement": settlement(conn, frm, to)}


def settlement(conn, from_date, to_date):
    """结算依据：按已完成会话汇总人数、材料消耗与师傅工时。"""
    frm, to = check_date(from_date, "from"), check_date(to_date, "to")
    lines = []
    for s in conn.execute(
            "SELECT * FROM sessions WHERE status='completed' AND date BETWEEN ? AND ? "
            "ORDER BY date, start_min", (frm, to)):
        bk = conn.execute("SELECT org_name FROM bookings WHERE id=?",
                          (s["booking_id"],)).fetchone()
        masters = conn.execute(
            "SELECT sm.master_id, m.name AS master_name, sm.skill FROM session_masters sm "
            "JOIN masters m ON m.id=sm.master_id WHERE sm.session_id=? AND sm.status IN "
            "('assigned','released')", (s["id"],)).fetchall()
        mats = conn.execute(
            "SELECT sm.material_id, mt.name, mt.unit, sm.qty_consumed FROM session_materials sm "
            "JOIN materials mt ON mt.id=sm.material_id WHERE sm.session_id=? "
            "AND sm.status='consumed'", (s["id"],)).fetchall()
        hours = round((s["end_min"] - s["start_min"]) / 60, 2)
        lines.append({"session_id": s["id"], "date": s["date"], "org_name": bk["org_name"],
                      "course_id": s["course_id"], "group_no": s["group_no"],
                      "attended": s["attended"],
                      "masters": [{**dict(m), "hours": hours} for m in masters],
                      "materials": [dict(m) for m in mats]})
    totals = {"sessions": len(lines), "attended": sum(l["attended"] for l in lines),
              "materials": {}, "master_hours": {}}
    for l in lines:
        for m in l["materials"]:
            t = totals["materials"].setdefault(m["material_id"],
                                               {"name": m["name"], "unit": m["unit"], "qty": 0})
            t["qty"] += m["qty_consumed"]
        for m in l["masters"]:
            t = totals["master_hours"].setdefault(m["master_id"],
                                                  {"name": m["master_name"], "hours": 0})
            t["hours"] = round(t["hours"] + m["hours"], 2)
    return {"from": frm, "to": to, "lines": lines, "totals": totals}
