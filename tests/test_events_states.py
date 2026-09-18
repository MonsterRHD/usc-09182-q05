"""迟到、取消、拆团、雨天转室、材料短缺、师傅请假 → 可解释状态。"""

import unittest

from service import core
from service.db import Store
from tests.helpers import DATE, confirm_first_plan, make_booking, seed_master_data


class EventStateTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.conn = self.store.conn
        seed_master_data(self.conn)

    def tearDown(self):
        self.store.close()

    def _confirmed_session(self, ref="R1", headcount=20):
        bk = make_booking(self.conn, ref=ref, headcount=headcount)
        confirm_first_plan(self.conn, bk["id"], headcount)
        return core.list_sessions(self.conn, booking_id=bk["id"])[0]

    def test_late_within_slot_slack(self):
        s = self._confirmed_session()
        out = core.report_late(self.conn, s["id"], {"minutes": 20})
        r = [x for x in out["state_reasons"] if x["code"] == "LATE_ARRIVAL"][0]
        self.assertEqual(r["facts"]["impact"], "extend")
        self.assertIn("顺延", r["message"])

    def test_late_beyond_slot_slack_compresses(self):
        s = self._confirmed_session()
        out = core.report_late(self.conn, s["id"], {"minutes": 240})
        r = [x for x in out["state_reasons"] if x["code"] == "LATE_ARRIVAL"][0]
        self.assertEqual(r["facts"]["impact"], "compress")
        self.assertIn("压缩", r["message"])

    def test_cancel_releases_resources(self):
        s = self._confirmed_session()
        core.cancel_booking(self.conn, s["booking_id"], {"reason": "学校临时调课"})
        mats = {m["id"]: m for m in core.list_materials(self.conn)}
        self.assertEqual(mats["clay"]["reserved"], 0)
        slots = {sl["id"]: sl for sl in core.list_slots(self.conn, date=DATE)}
        self.assertTrue(all(sl["used"] == 0 for sl in slots.values()))
        # 幂等：重复取消返回相同状态
        again = core.cancel_booking(self.conn, s["booking_id"], {})
        self.assertEqual(again["status"], "cancelled")
        # 取消产生可解释状态
        self.assertTrue(any(r["code"] == "CANCELLED" for r in again["state_reasons"]))

    def test_split_moves_part_to_new_slot(self):
        """35 人团确认后拆出 15 人：原组 20 人，新组另找时段与师傅。"""
        bk = make_booking(self.conn, headcount=35)
        confirm_first_plan(self.conn, bk["id"], 35)
        sessions = core.list_sessions(self.conn, booking_id=bk["id"])
        target = [s for s in sessions if s["headcount"] == 18][0]
        out = core.split_session(self.conn, target["id"], {"parts": [10, 8]})
        self.assertEqual(out["original"]["headcount"], 10)
        self.assertEqual(out["new_session"]["headcount"], 8)
        self.assertEqual(out["new_session"]["status"], "confirmed")
        self.assertTrue(out["new_session"]["masters"])
        r = [x for x in out["original"]["state_reasons"] if x["code"] == "SPLIT_GROUP"]
        self.assertTrue(r)
        # 拆出后材料总额不变
        mats = {m["id"]: m for m in core.list_materials(self.conn)}
        self.assertEqual(mats["clay"]["reserved"], 35)

    def test_split_rejects_bad_parts(self):
        s = self._confirmed_session()
        with self.assertRaises(core.ApiError) as ctx:
            core.split_session(self.conn, s["id"], {"parts": [10, 5]})
        self.assertEqual(ctx.exception.code, "PARTS_MISMATCH")

    def test_rain_transfer_to_indoor(self):
        """雨天转室：室外 → 室内同时段，时间不变并留痕。"""
        s = self._confirmed_session()
        self.assertEqual(s["venue_kind"], "outdoor")
        out = core.transfer_session(self.conn, s["id"], {"reason": "rain"})
        self.assertEqual(out["venue_kind"], "indoor")
        self.assertEqual(out["start"], s["start"])
        r = [x for x in out["state_reasons"] if x["code"] == "MOVED_INDOOR_RAIN"]
        self.assertTrue(r)
        # 原室外时段容量释放，室内时段被占用
        slots = {sl["id"]: sl for sl in core.list_slots(self.conn, date=DATE)}
        self.assertEqual(slots[s["slot_id"]]["used"], 0)
        self.assertEqual(slots[out["slot_id"]]["used"], 20)

    def test_rain_transfer_fails_explainably_when_no_indoor(self):
        """室内装不下时转室失败，但状态可解释。"""
        big = make_booking(self.conn, ref="R2", headcount=40, course="pottery1")
        # 40 人拆两组 20+20；把室内容量调到 15 让转室必然失败
        self.conn.execute("UPDATE slots SET capacity=15 WHERE venue_id='hall'")
        confirm_first_plan(self.conn, big["id"], 40)
        sessions = core.list_sessions(self.conn, booking_id=big["id"])
        with self.assertRaises(core.ApiError) as ctx:
            core.transfer_session(self.conn, sessions[0]["id"], {"reason": "rain"})
        self.assertEqual(ctx.exception.code, "NO_INDOOR_VENUE")
        out = core.get_session(self.conn, sessions[0]["id"])
        self.assertTrue([x for x in out["state_reasons"] if x["code"] == "TRANSFER_FAILED"])

    def test_material_shortage_marks_future_sessions(self):
        """材料临时短缺：缺口落在未开始的会话上并给出缺口数。"""
        bk1 = make_booking(self.conn, ref="R1", headcount=20)
        confirm_first_plan(self.conn, bk1["id"], 20)
        bk2 = make_booking(self.conn, ref="R2", headcount=20)
        confirm_first_plan(self.conn, bk2["id"], 20)
        # 库存 60 → 25，预留 40，缺口 15 落在较晚的会话上
        out = core.adjust_material(self.conn, "clay", {"delta": -35, "reason": "运输损毁"})
        self.assertEqual(len(out["affected_sessions"]), 1)
        affected_id = out["affected_sessions"][0]["session_id"]
        s = core.get_session(self.conn, affected_id)
        r = [x for x in s["state_reasons"] if x["code"] == "MATERIAL_SHORTAGE"][0]
        self.assertEqual(r["facts"]["gap"], 15)
        # 补足库存后短缺标记自动解除
        core.adjust_material(self.conn, "clay", {"delta": +35, "reason": "补货到"})
        s = core.get_session(self.conn, affected_id)
        self.assertFalse([x for x in s["state_reasons"] if x["code"] == "MATERIAL_SHORTAGE"])

    def test_master_leave_auto_reassign(self):
        """制坯师请假：同技能师傅自动改派，留痕可查。"""
        s = self._confirmed_session()
        out = core.master_leave(self.conn, "pot1", {
            "start_at": f"{DATE}T08:00:00", "end_at": f"{DATE}T12:00:00",
            "reason": "家中急事"})
        self.assertEqual(len(out["reassignments"]), 1)
        self.assertEqual(out["reassignments"][0]["to_master_id"], "pot2")
        after = core.get_session(self.conn, s["id"])
        skills = {m["skill"]: m for m in after["masters"] if m["status"] == "assigned"}
        self.assertEqual(skills["pottery"]["master_id"], "pot2")
        self.assertTrue([x for x in after["state_reasons"]
                         if x["code"] == "MASTER_REASSIGNED"])

    def test_master_leave_unassigned_when_no_backup(self):
        """讲解员只有一名，请假后无人能顶 → MASTER_UNASSIGNED 可解释状态。"""
        s = self._confirmed_session()
        out = core.master_leave(self.conn, "doc1", {
            "start_at": f"{DATE}T08:00:00", "end_at": f"{DATE}T12:00:00"})
        self.assertEqual(len(out["unassigned"]), 1)
        after = core.get_session(self.conn, s["id"])
        r = [x for x in after["state_reasons"] if x["code"] == "MASTER_UNASSIGNED"]
        self.assertTrue(r)
        self.assertIn("需人工协调", r[0]["message"])


if __name__ == "__main__":
    unittest.main()
