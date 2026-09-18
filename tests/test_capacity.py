"""研学体验容量管家 —— 领域测试。

覆盖：双节高峰容量、迟到、取消、拆团、雨天转室、材料短缺、
师傅请假改派、跨日课程、已开始体验资源保护、幂等重试、
崩溃恢复交接、复盘回放与结算依据。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import HTTPServer

from service.app import CapacityService, ServiceError
from service.domain import (
    INC_MATERIAL_SHORT, INC_PARTIAL_KITS, INC_RAIN_NO_ROOM,
    INC_RAIN_PROTECTED, INC_RAIN_RELOCATED, INC_STAFF_SHORTAGE,
    ST_CANCELLED, ST_COMPLETED, ST_CONFIRMED, ST_IN_PROGRESS, ST_LATE,
    ST_SPLIT, ST_TERMINATED,
)
from service.main import Handler
from service.store import EventStore, replay

DAY = "2026-10-01"
DAY2 = "2026-10-02"


def make_service(stock_a=200, n_guides=4, n_makers=8, venues=None,
                 multiday=False) -> CapacityService:
    svc = CapacityService(EventStore())
    pattern = ((0, 0), (1, 0)) if multiday else ((0, 0),)
    svc.register_course("c1", "NIHU", 1, "泥塑基础", "NI", "KIT-A",
                        per_maker_ratio=6, max_group_size=20,
                        pattern=pattern, unit_fee_cents=500)
    svc.register_course("c2", "JIANZHI", 1, "剪纸入门", "JZ", "KIT-B",
                        per_maker_ratio=10, max_group_size=30,
                        venue_kind="INDOOR", alt_skus=("KIT-A",),
                        unit_fee_cents=300)
    venues = venues or [("V1", "室内工坊一", True, 20),
                        ("V2", "室内工坊二", True, 20),
                        ("V3", "室外展场", False, 60)]
    for i, (vid, name, indoor, cap) in enumerate(venues):
        svc.register_venue(f"v{i}", vid, name, indoor, cap)
    for i in range(n_guides):
        svc.register_master(f"g{i}", f"G{i}", f"讲解{i}", {"GUIDE"})
    for i in range(n_makers):
        sk = {"NI"} if i % 2 == 0 else {"NI", "JZ"}
        svc.register_master(f"m{i}", f"M{i}", f"师傅{i}", sk)
    svc.receive_stock("s1", "KIT-A", stock_a)
    svc.receive_stock("s2", "KIT-B", stock_a)
    return svc


def book_and_confirm(svc: CapacityService, students: int, day=DAY, slot=0,
                     course="NIHU", booking_id=None, key_prefix="b",
                     rain=False):
    r = svc.receive_team(f"{key_prefix}-team", booking_id, course,
                         day, slot, students, rain_indoor=rain)
    if r["feasible"]:
        c = svc.confirm_plan(f"{key_prefix}-confirm", r["plan_id"])
        return r, c["sessions"]
    return r, []


class HolidayPeakTest(unittest.TestCase):
    def test_overbooking_explained_with_alternatives(self):
        # 室内两馆共 40 座；高峰连排，第 41 人排不进同时段
        svc = make_service(n_guides=10, n_makers=20, stock_a=500,
                           venues=[("V1", "室内工坊一", True, 20),
                                   ("V2", "室内工坊二", True, 20)])
        _, s1 = book_and_confirm(svc, 20, key_prefix="a")
        _, s2 = book_and_confirm(svc, 20, key_prefix="b")
        self.assertEqual(len(s1 + s2), 2)  # 两馆 40 座占满

        plan, sessions = book_and_confirm(svc, 10, key_prefix="c")
        self.assertEqual(sessions, [])
        self.assertFalse(plan["feasible"])
        self.assertEqual(plan["shortage"], 10)
        codes = {reason for row in plan["reasons"] for reason in row["reasons"]}
        self.assertIn("VENUE_FULL", codes)
        # 替代时段：同日其他时段必然可行
        self.assertTrue(plan["alternatives"])
        self.assertEqual(plan["alternatives"][0]["start_date"], DAY)

    def test_temporary_group_gets_full_package(self):
        svc = make_service()
        plan, sessions = book_and_confirm(svc, 18, key_prefix="x")
        self.assertTrue(plan["feasible"])
        sid = sessions[0]
        view = svc.session_view(sid)
        self.assertEqual(view["size"], 18)
        self.assertTrue(view["venues"])
        self.assertTrue(view["staff"])
        self.assertEqual(view["material_sku"], "KIT-A")


class LateCancelSplitTest(unittest.TestCase):
    def test_late_team_keeps_resources(self):
        svc = make_service(n_guides=2, n_makers=4)
        _, sessions = book_and_confirm(svc, 20, key_prefix="a")
        sid = sessions[0]
        svc.mark_late("late1", sid, note="堵车")
        self.assertEqual(svc.session_view(sid)["status"], ST_LATE)
        # 高峰再插一团：迟到团的场地/师傅/材料都不能被抢走
        plan2, s2 = book_and_confirm(svc, 20, key_prefix="b")
        self.assertFalse(plan2["feasible"])
        # 晚到后仍可正常开场
        svc.start_session("start1", sid)
        self.assertEqual(svc.session_view(sid)["status"], ST_IN_PROGRESS)

    def test_cancel_releases_everything(self):
        svc = make_service(n_guides=10, n_makers=20, stock_a=40)
        _, sessions = book_and_confirm(svc, 20, key_prefix="a")
        sid = sessions[0]
        before = svc.stock_view()["KIT-A"]["held"]
        self.assertEqual(before, 20)
        svc.cancel_session("cancel1", sid, reason="学校临时取消")
        self.assertEqual(svc.session_view(sid)["status"], ST_CANCELLED)
        self.assertEqual(svc.stock_view()["KIT-A"]["held"], 0)
        # 释放后新团可排（材料恰好 20）
        plan2, s2 = book_and_confirm(svc, 20, key_prefix="b")
        self.assertTrue(plan2["feasible"])

    def test_cannot_cancel_started(self):
        svc = make_service()
        _, sessions = book_and_confirm(svc, 10, key_prefix="a")
        svc.start_session("s", sessions[0])
        with self.assertRaises(ServiceError) as ctx:
            svc.cancel_session("c", sessions[0])
        self.assertEqual(ctx.exception.code, "BAD_STATUS")

    def test_split_team_atomic_and_tracked(self):
        svc = make_service(n_guides=4, n_makers=8)
        _, sessions = book_and_confirm(svc, 20, key_prefix="a")
        parent = sessions[0]
        result = svc.split_team("split1", parent, [
            {"size": 12}, {"size": 8, "rain_indoor": True}])
        self.assertEqual(len(result["children"]), 2)
        self.assertEqual(svc.session_view(parent)["status"], ST_SPLIT)
        child_views = [svc.session_view(c) for c in result["children"]]
        self.assertEqual(sum(v["size"] for v in child_views), 20)
        self.assertEqual(svc.session_view(parent)["child_ids"],
                         result["children"])
        for v in child_views:
            self.assertEqual(v["parent_id"], parent)
            self.assertEqual(v["status"], ST_CONFIRMED)

    def test_split_size_mismatch_rolls_back(self):
        svc = make_service()
        _, sessions = book_and_confirm(svc, 20, key_prefix="a")
        events_before = len(svc.store.all())
        with self.assertRaises(ServiceError):
            svc.split_team("splitbad", sessions[0], [{"size": 12}, {"size": 7}])
        self.assertEqual(len(svc.store.all()), events_before)
        self.assertEqual(svc.session_view(sessions[0])["status"], ST_CONFIRMED)


class RainTest(unittest.TestCase):
    def test_rain_moves_unstarted_outdoor_to_indoor(self):
        svc = make_service()
        # 室外课程：直接构造一场占用室外展场的团
        svc.register_course("cout", "OUT", 1, "拉坯户外", "NI", "KIT-A",
                            venue_kind="OUTDOOR", max_group_size=20)
        _, sessions = book_and_confirm(svc, 15, course="OUT", key_prefix="o")
        sid = sessions[0]
        sk = f"{DAY}|0"
        self.assertFalse(_venue_indoor(svc, sid, sk))

        result = svc.rain_switch("rain1", DAY, 0)
        self.assertEqual([m["session_id"] for m in result["moved"]], [sid])
        self.assertTrue(_venue_indoor(svc, sid, sk))
        codes = {i["code"] for i in svc.incidents(sid)}
        self.assertIn(INC_RAIN_RELOCATED, codes)

    def test_started_session_protected_from_rain_move(self):
        svc = make_service()
        svc.register_course("cout", "OUT", 1, "拉坯户外", "NI", "KIT-A",
                            venue_kind="OUTDOOR", max_group_size=20)
        _, sessions = book_and_confirm(svc, 15, course="OUT", key_prefix="o")
        sid = sessions[0]
        svc.start_session("st", sid)
        result = svc.rain_switch("rain1", DAY, 0)
        self.assertEqual(result["protected"], [sid])
        self.assertEqual(result["moved"], [])
        codes = {i["code"] for i in svc.incidents(sid)}
        self.assertIn(INC_RAIN_PROTECTED, codes)

    def test_rain_without_indoor_room_is_explained(self):
        # 只有室外场地，下雨无处可转
        svc = make_service(venues=[("V3", "室外展场", False, 60)])
        svc.register_course("cout", "OUT", 1, "拉坯户外", "NI", "KIT-A",
                            venue_kind="OUTDOOR", max_group_size=20)
        _, sessions = book_and_confirm(svc, 15, course="OUT", key_prefix="o")
        result = svc.rain_switch("rain1", DAY, 0)
        self.assertEqual(result["no_room"], sessions)
        codes = {i["code"] for i in svc.incidents(sessions[0])}
        self.assertIn(INC_RAIN_NO_ROOM, codes)

    def test_rain_relocates_multiple_teams_without_double_count(self):
        # 室内一馆 15 座、二馆 15 座；两个 12 人室外团应各转一馆，不重复占位
        svc = make_service(n_guides=4, n_makers=8,
                           venues=[("V1", "室内工坊一", True, 15),
                                   ("V2", "室内工坊二", True, 15),
                                   ("V3", "室外展场", False, 60)])
        svc.register_course("cout", "OUT", 1, "拉坯户外", "NI", "KIT-A",
                            venue_kind="OUTDOOR", max_group_size=20)
        _, s1 = book_and_confirm(svc, 12, course="OUT", key_prefix="o1")
        _, s2 = book_and_confirm(svc, 12, course="OUT", key_prefix="o2")
        result = svc.rain_switch("rain1", DAY, 0)
        moved_venues = {m["session_id"]: m["venue_id"] for m in result["moved"]}
        self.assertEqual(set(moved_venues), {s1[0], s2[0]})
        self.assertEqual(len(set(moved_venues.values())), 2)  # 不挤在同一馆
        # 第三个 6 人团此时两馆都还剩 3 座，转不进，给可解释状态
        _, s3 = book_and_confirm(svc, 6, course="OUT", key_prefix="o3")
        result2 = svc.rain_switch("rain2", DAY, 0)
        self.assertEqual(result2["no_room"], s3)


class MaterialShortageTest(unittest.TestCase):
    def test_stock_adjustment_flags_affected_teams(self):
        svc = make_service(stock_a=15)
        _, sessions = book_and_confirm(svc, 15, key_prefix="a")
        sid = sessions[0]
        result = svc.adjust_stock("adj1", "KIT-A", -10, reason="物料受潮")
        self.assertEqual(result["shortages"][0]["session_id"], sid)
        self.assertEqual(result["shortages"][0]["covered"], 5)
        codes = {i["code"] for i in svc.incidents(sid)}
        self.assertIn(INC_MATERIAL_SHORT, codes)

    def test_start_with_partial_kits(self):
        svc = make_service(stock_a=15)
        _, sessions = book_and_confirm(svc, 15, key_prefix="a")
        sid = sessions[0]
        svc.adjust_stock("adj1", "KIT-A", -10)
        result = svc.start_session("start1", sid)
        self.assertEqual(result["attended"], 15)
        self.assertEqual(result["consumed"], 5)
        self.assertIn(INC_PARTIAL_KITS, result["incidents"])

    def test_alternative_material_sku_used(self):
        svc = make_service()
        svc.adjust_stock("adj", "KIT-B", -200)
        r = svc.receive_team("t1", "B1", "JIANZHI", DAY, 0, 10)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["options"][0]["material_sku"], "KIT-A")

    def test_kits_follow_confirmation_order_and_absence_passes_through(self):
        # 确认后总量降到 18：先确认的团先得；先团有人缺席，未领的包顺延给后团
        svc = make_service(stock_a=30, n_guides=10, n_makers=20,
                           venues=[("V1", "工坊", True, 40)])
        _, s1 = book_and_confirm(svc, 15, key_prefix="a")
        _, s2 = book_and_confirm(svc, 15, key_prefix="b")
        svc.adjust_stock("adj", "KIT-A", -12)
        codes1 = [r["code"] for r in svc.session_view(s1[0])["roster"]]
        # 先团 15 包配额，实到 13 人，只耗 13
        r1 = svc.start_session("st1", s1[0], attended_codes=codes1[:13])
        self.assertEqual(r1["consumed"], 13)
        self.assertEqual(r1["incidents"], [])
        svc.complete_session("done1", s1[0])
        # 后团可用 = 18 - 13 = 5，15 人到场只能发 5 包并给出异常
        r2 = svc.start_session("st2", s2[0])
        self.assertEqual(r2["attended"], 15)
        self.assertEqual(r2["consumed"], 5)
        self.assertIn(INC_PARTIAL_KITS, r2["incidents"])


class MasterLeaveTest(unittest.TestCase):
    def test_leave_auto_reassigns_before_start(self):
        svc = make_service()
        _, sessions = book_and_confirm(svc, 10, key_prefix="a")
        sid = sessions[0]
        sk = f"{DAY}|0"
        maker_before = _makers(svc, sid, sk)
        leaver = maker_before[0]
        result = svc.register_leave("leave1", leaver, DAY, 0, reason="家中急事")
        effects = {e["session_id"]: e for e in result["reassigned"]}
        self.assertIsNotNone(effects[sid]["replacement"])
        maker_after = _makers(svc, sid, sk)
        self.assertNotIn(leaver, maker_after)

    def test_leave_without_replacement_blocks_start(self):
        # 只有一名制坯师能覆盖该团（12 人以下只需 1 名制坯师）
        svc = make_service(n_guides=1, n_makers=1)
        _, sessions = book_and_confirm(svc, 6, key_prefix="a")
        sid = sessions[0]
        sk = f"{DAY}|0"
        leaver = _makers(svc, sid, sk)[0]
        result = svc.register_leave("leave1", leaver, DAY, 0)
        self.assertIsNone(result["reassigned"][0]["replacement"])
        codes = {i["code"] for i in svc.incidents(sid)}
        self.assertIn(INC_STAFF_SHORTAGE, codes)
        with self.assertRaises(ServiceError) as ctx:
            svc.start_session("start1", sid)
        self.assertEqual(ctx.exception.code, "STAFF_UNAVAILABLE")

    def test_leave_then_cancel_leave_keeps_history(self):
        svc = make_service()
        _, sessions = book_and_confirm(svc, 6, key_prefix="a")
        leaver = _makers(svc, sessions[0], f"{DAY}|0")[0]
        svc.register_leave("l1", leaver, DAY, 0)
        svc.cancel_leave("l2", leaver, DAY, 0)
        # 事件仍保留在流中，复盘可查
        types = [e["type"] for e in svc.store.all()]
        self.assertIn("MasterLeaveRegistered", types)
        self.assertIn("MasterLeaveCancelled", types)


class StartedProtectionTest(unittest.TestCase):
    def test_later_booking_cannot_take_started_resources(self):
        svc = make_service(n_guides=1, n_makers=2, stock_a=100,
                           venues=[("V1", "室内工坊", True, 20)])
        _, sessions = book_and_confirm(svc, 12, key_prefix="a")
        sid = sessions[0]
        svc.start_session("st", sid)
        # 后来的排程：场地只剩 8 座、讲解已被占 → 全员不可行
        plan, more = book_and_confirm(svc, 12, key_prefix="b")
        self.assertFalse(plan["feasible"])
        self.assertEqual(more, [])
        # 已开始的团照常完成，资源没被挪走
        svc.complete_session("done", sid)
        self.assertEqual(svc.session_view(sid)["status"], ST_COMPLETED)

    def test_stock_adjustment_cannot_erase_consumed(self):
        svc = make_service(stock_a=20)
        _, sessions = book_and_confirm(svc, 10, key_prefix="a")
        sid = sessions[0]
        svc.start_session("st", sid)  # 已消耗 10
        with self.assertRaises(ServiceError) as ctx:
            svc.adjust_stock("bad", "KIT-A", -15)
        self.assertEqual(ctx.exception.code, "STOCK_NEGATIVE")


class MultidayCourseTest(unittest.TestCase):
    def test_cross_day_course_occupies_both_days(self):
        svc = make_service(multiday=True, n_guides=1, n_makers=2)
        plan, sessions = book_and_confirm(svc, 10, key_prefix="a")
        self.assertTrue(plan["feasible"])
        sid = sessions[0]
        slots = svc.session_view(sid)["slots"]
        self.assertEqual([s[0] for s in slots], [DAY, DAY2])
        # 第二天同时段别人排不进同一批资源（讲解/师傅被跨日团占用）
        plan2, _ = book_and_confirm(svc, 10, day=DAY2, key_prefix="b")
        self.assertFalse(plan2["feasible"])
        svc.start_session("st", sid)
        handover = svc.handover(DAY2)
        ids = [x["session_id"] for x in handover["open_sessions"]]
        self.assertIn(sid, ids)
        svc.complete_session("done", sid)
        settlement = svc.session_view(sid)["settlement"]
        self.assertEqual(settlement["students_attended"], 10)
        self.assertEqual(settlement["fee_cents"], 5000)


class RosterTest(unittest.TestCase):
    def test_roster_keeps_minimal_info(self):
        svc = make_service()
        _, sessions = book_and_confirm(svc, 3, key_prefix="a")
        sid = sessions[0]
        svc.save_roster("r1", sid, ["S001", "S002", "S003"])
        roster = svc.session_view(sid)["roster"]
        self.assertEqual({r["code"] for r in roster}, {"S001", "S002", "S003"})
        for row in roster:
            self.assertEqual(set(row.keys()), {"code", "attended"})

    def test_roster_rejects_bad_size_and_duplicates(self):
        svc = make_service()
        _, sessions = book_and_confirm(svc, 3, key_prefix="a")
        sid = sessions[0]
        with self.assertRaises(ServiceError) as ctx:
            svc.save_roster("r1", sid, ["A", "B"])
        self.assertEqual(ctx.exception.code, "ROSTER_SIZE_MISMATCH")
        with self.assertRaises(ServiceError) as ctx:
            svc.save_roster("r2", sid, ["A", "A", "B"])
        self.assertEqual(ctx.exception.code, "ROSTER_DUPLICATE")
        # 开场后名单锁定
        svc.start_session("st", sid)
        with self.assertRaises(ServiceError) as ctx:
            svc.save_roster("r3", sid, ["A", "B", "C"])
        self.assertEqual(ctx.exception.code, "ROSTER_LOCKED")

    def test_roster_retry_is_idempotent(self):
        svc = make_service()
        _, sessions = book_and_confirm(svc, 2, key_prefix="a")
        sid = sessions[0]
        svc.save_roster("same-key", sid, ["A", "B"])
        svc.save_roster("same-key", sid, ["A", "B"])
        svc.save_roster("same-key", sid, ["A", "B"])
        save_events = [e for e in svc.store.all() if e["type"] == "RosterSaved"]
        self.assertEqual(len(save_events), 1)


class SettlementTest(unittest.TestCase):
    def test_settlement_basis_attended_and_terminated(self):
        svc = make_service()
        _, sessions = book_and_confirm(svc, 10, key_prefix="a")
        sid = sessions[0]
        codes = svc.session_view(sid)["roster"]
        all_codes = [c["code"] for c in codes]
        # 2 人缺席
        result = svc.start_session("st", sid, attended_codes=all_codes[:8])
        self.assertEqual(result["attended"], 8)
        svc.complete_session("done", sid)
        stl = svc.session_view(sid)["settlement"]
        self.assertEqual(stl["basis"], "ATTENDED")
        self.assertEqual(stl["students_planned"], 10)
        self.assertEqual(stl["students_attended"], 8)
        self.assertEqual(stl["fee_cents"], 4000)
        self.assertEqual(stl["material_consumed"], 8)

    def test_early_terminate_settles_by_actual(self):
        svc = make_service()
        _, sessions = book_and_confirm(svc, 6, key_prefix="a")
        sid = sessions[0]
        svc.start_session("st", sid)
        svc.terminate_session("term", sid, reason="身体不适")
        self.assertEqual(svc.session_view(sid)["status"], ST_TERMINATED)
        stl = svc.session_view(sid)["settlement"]
        self.assertEqual(stl["students_attended"], 6)
        self.assertEqual(stl["note"], "身体不适")


class IdempotencyAndRecoveryTest(unittest.TestCase):
    def test_channel_retry_returns_first_result(self):
        svc = make_service()
        payload_args = ("channel-key-1", "BKG-9", "NIHU", DAY, 0, 12)
        r1 = svc.receive_team(*payload_args)
        n_after_first = len(svc.store.all())
        r2 = svc.receive_team(*payload_args)
        r3 = svc.receive_team(*payload_args)
        self.assertEqual(r1["plan_id"], r2["plan_id"], r3["plan_id"])
        self.assertEqual(len(svc.store.all()), n_after_first)

    def test_failed_command_rolls_back_and_is_retryable(self):
        svc = make_service()
        with self.assertRaises(ServiceError):
            svc.confirm_plan("bad", "PLAN-404")
        # 失败后服务仍可正常处理同键/新请求
        r = svc.receive_team("t1", "B1", "NIHU", DAY, 0, 6)
        self.assertTrue(r["feasible"])

    def test_restart_restores_state_and_counters(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.jsonl")
            svc = CapacityService(EventStore(path))
            svc.register_course("c1", "NIHU", 1, "泥塑", "NI", "KIT-A",
                                max_group_size=20)
            svc.register_venue("v1", "V1", "室内", True, 30)
            svc.register_master("g1", "G1", "讲解", {"GUIDE"})
            svc.register_master("m1", "M1", "师傅", {"NI"})
            svc.receive_stock("s1", "KIT-A", 50)
            r = svc.receive_team("t1", "B1", "NIHU", DAY, 0, 6)
            svc.confirm_plan("cf1", r["plan_id"])
            max_seq = len(svc.store.all())

            # 模拟服务重启
            svc2 = CapacityService(EventStore(path))
            self.assertEqual(len(svc2.store.all()), max_seq)
            handover = svc2.handover(DAY)
            self.assertEqual(len(handover["open_sessions"]), 1)
            # 计数器续号，不与既有会话冲突
            r2 = svc2.receive_team("t2", "B2", "NIHU", DAY, 1, 6)
            self.assertTrue(r2["feasible"])
            new_sids = [o["session_id"] for o in r2["options"]]
            existing = set(svc2.state.sessions)
            self.assertFalse(set(new_sids) & existing)

    def test_same_day_open_sessions_handed_over(self):
        svc = make_service()
        _, s1 = book_and_confirm(svc, 6, key_prefix="a")
        _, s2 = book_and_confirm(svc, 6, slot=1, key_prefix="b")
        svc.start_session("st", s1[0])
        handover = svc.handover(DAY)
        statuses = {x["session_id"]: x["status"] for x in handover["open_sessions"]}
        self.assertEqual(statuses[s1[0]], ST_IN_PROGRESS)
        self.assertEqual(statuses[s2[0]], ST_CONFIRMED)


class ReplayReviewTest(unittest.TestCase):
    def test_replay_until_seq_reconstructs_history(self):
        svc = make_service()
        _, sessions = book_and_confirm(svc, 6, key_prefix="a")
        sid = sessions[0]
        seq_after_confirm = len(svc.store.all())
        svc.start_session("st", sid)
        svc.complete_session("done", sid)

        past = svc.replay_until(seq_after_confirm)
        self.assertEqual(past.sessions[sid].status, ST_CONFIRMED)
        self.assertIsNone(past.sessions[sid].settlement)
        # 当前状态不受复盘影响
        self.assertEqual(svc.session_view(sid)["status"], ST_COMPLETED)

    def test_replay_stock_and_incidents(self):
        svc = make_service(stock_a=15)
        _, sessions = book_and_confirm(svc, 15, key_prefix="a")
        seq = len(svc.store.all())
        svc.adjust_stock("adj", "KIT-A", -10)
        past = replay(svc.store.all(), until_seq=seq)
        self.assertEqual(past.stock["KIT-A"].total, 15)
        self.assertEqual(past.incidents, [])
        self.assertEqual(len(svc.incidents()), 1)


class HttpApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["EVENT_FILE"] = os.path.join(self.tmp.name, "events.jsonl")
        import service.main as main
        main.SERVICE = None
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.tmp.cleanup()
        os.environ.pop("EVENT_FILE", None)
        import service.main as main
        main.SERVICE = None

    def _post(self, path, body, key=None):
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers={"Content-Type": "application/json",
                     **({"Idempotency-Key": key} if key else {})})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
            return json.loads(r.read())

    def test_http_flow_and_retry(self):
        status, _ = self._post("/admin/courses", {
            "course_code": "NIHU", "version": 1, "title": "泥塑",
            "maker_skill": "NI", "material_sku": "KIT-A",
            "max_group_size": 20}, key="c1")
        self.assertEqual(status, 200)
        self._post("/admin/venues", {"venue_id": "V1", "name": "室内",
                                     "indoor": True, "capacity": 30}, key="v1")
        self._post("/admin/masters", {"master_id": "G1", "name": "讲解",
                                      "skills": ["GUIDE"]}, key="g1")
        self._post("/admin/masters", {"master_id": "M1", "name": "师傅一",
                                      "skills": ["NI"]}, key="m1")
        self._post("/admin/masters", {"master_id": "M2", "name": "师傅二",
                                      "skills": ["NI"]}, key="m2")
        self._post("/admin/stock/receive", {"sku": "KIT-A", "qty": 50}, key="s1")

        status, r1 = self._post("/teams", {
            "booking_id": "B1", "course_code": "NIHU", "start_date": DAY,
            "start_slot": 0, "students": 12}, key="team-1")
        self.assertEqual(status, 200)
        _, r2 = self._post("/teams", {
            "booking_id": "B1", "course_code": "NIHU", "start_date": DAY,
            "start_slot": 0, "students": 12}, key="team-1")
        self.assertEqual(r1["plan_id"], r2["plan_id"])

        status, cf = self._post(f"/plans/{r1['plan_id']}/confirm", {}, key="cf1")
        self.assertEqual(status, 200)
        sid = cf["sessions"][0]
        _, st = self._post(f"/sessions/{sid}/start", {}, key="st1")
        self.assertEqual(st["status"], ST_IN_PROGRESS)

        sched = self._get(f"/schedule?date={DAY}")
        self.assertEqual(len(sched["sessions"]), 1)
        handover = self._get(f"/handover?date={DAY}")
        self.assertEqual(handover["open_sessions"][0]["session_id"], sid)
        health = self._get("/health")
        self.assertEqual(health["status"], "ok")


# ---- 测试辅助 -------------------------------------------------------------

def _venue_indoor(svc: CapacityService, sid: str, sk: str) -> bool:
    venue_id = svc.session_view(sid)["venues"][sk]
    # 通过排程视图判断：直接查重建后的状态
    return venue_id in {"V1", "V2"}


def _makers(svc: CapacityService, sid: str, sk: str) -> list[str]:
    staff = svc.session_view(sid)["staff"][sk]
    return [mid for role, mid in staff.items() if role != "GUIDE"]


# ---- 业务复盘端到端场景 ---------------------------------------------------

class PeakReplayScenarioTest(unittest.TestCase):
    def test_double_festival_peak_leave_and_multiday_replay(self):
        svc = make_service(n_guides=2, n_makers=4, multiday=True, stock_a=120,
                           venues=[("V1", "综合工坊", True, 20)])
        national_day, oct2 = DAY, DAY2

        # 双节高峰：上午两场连排，先到的两团各 12 人（跨日课程）
        _, a_sessions = book_and_confirm(svc, 12, day=national_day, slot=0, key_prefix="a")
        _, b_sessions = book_and_confirm(svc, 12, day=national_day, slot=1, key_prefix="b")

        # 临时加团 12 人：容量/人手不足，方案给原因和替代
        rush = svc.receive_team("rush", "B-RUSH", "NIHU", national_day, 0, 12)
        self.assertFalse(rush["feasible"])
        self.assertTrue(rush["reasons"])
        self.assertTrue(rush["alternatives"])

        # 师傅临时请假：自动改派 A 团
        sid_a = a_sessions[0]
        sk0 = f"{national_day}|0"
        leaver = _makers(svc, sid_a, sk0)[0]
        heal = svc.register_leave("leave", leaver, national_day, 0)
        self.assertIsNotNone(heal["reassigned"][0]["replacement"])

        # A 团迟到，资源保留；之后开场（1 人缺席）并完成
        svc.mark_late("late", sid_a, note="接驳车晚点")
        codes = [r["code"] for r in svc.session_view(sid_a)["roster"]]
        svc.start_session("starta", sid_a, attended_codes=codes[:11])
        seq_at_start = len(svc.store.all())
        svc.complete_session("donea", sid_a)

        # B 团跨日：第二天继续，开场完成
        sid_b = b_sessions[0]
        self.assertEqual([x[0] for x in svc.session_view(sid_b)["slots"]],
                         [national_day, oct2])
        svc.start_session("startb", sid_b)
        svc.complete_session("doneb", sid_b)

        # ---- 复盘：回放至 A 团开场后、完成前 ----
        past = svc.replay_until(seq_at_start)
        self.assertEqual(past.sessions[sid_a].status, ST_IN_PROGRESS)
        self.assertIsNone(past.sessions[sid_a].settlement)
        # 请假与改派事件都在历史里
        codes_hist = [e["type"] for e in svc.store.all()]
        self.assertIn("MasterLeaveRegistered", codes_hist)
        self.assertIn("MakersReassigned", codes_hist)

        # ---- 结算依据：A 按 11 名到场、B 按 12 名 ----
        stl_a = svc.session_view(sid_a)["settlement"]
        stl_b = svc.session_view(sid_b)["settlement"]
        self.assertEqual((stl_a["students_attended"], stl_a["fee_cents"],
                          stl_a["material_consumed"]), (11, 5500, 11))
        self.assertEqual((stl_b["students_attended"], stl_b["fee_cents"]), (12, 6000))

        # 恢复交接：当天活动全部结束，交接清单为空
        self.assertEqual(svc.handover(national_day)["open_sessions"], [])


if __name__ == "__main__":
    unittest.main()
