"""业务复盘：双节高峰回放、师傅临时请假改派、跨日课程、结算依据核对。"""

import unittest

from service import core
from service.db import Store
from tests.helpers import DATE, confirm_first_plan, make_booking, seed_master_data

DAY2 = "2026-10-02"


class ReplaySettlementTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.conn = self.store.conn
        seed_master_data(self.conn)

    def tearDown(self):
        self.store.close()

    def test_peak_day_replay_capacity(self):
        """双节高峰：多单确认后回放，容量使用与事件时间线完整。"""
        for i, hc in enumerate([20, 20, 18]):
            bk = make_booking(self.conn, ref=f"PEAK{i}", headcount=hc)
            confirm_first_plan(self.conn, bk["id"], hc)
        replay = core.replay(self.conn, DATE, DATE)
        types = [e["type"] for e in replay["events"]]
        self.assertEqual(types.count("BOOKING_CONFIRMED"), 3)
        yard_slots = [c for c in replay["capacity"] if "大师工坊" in c["venue_name"]]
        self.assertTrue(any(c["used"] > 0 for c in yard_slots))
        for c in replay["capacity"]:
            self.assertFalse(c["overbooked"], f"{c} 出现超订")
            self.assertEqual(c["free"], c["capacity"] - c["used"])

    def test_master_leave_reassignment_replay(self):
        """师傅临时请假：自动改派，回放中能看到请假与改派记录。"""
        bk = make_booking(self.conn, headcount=20)
        confirm_first_plan(self.conn, bk["id"], 20)
        result = core.master_leave(self.conn, "pot1", {
            "start_at": f"{DATE}T08:00:00", "end_at": f"{DATE}T12:00:00",
            "reason": "临时身体不适"})
        self.assertEqual(len(result["reassignments"]), 1)
        self.assertEqual(result["reassignments"][0]["to_master_id"], "pot2")
        s = core.list_sessions(self.conn, booking_id=bk["id"])[0]
        skills = {m["skill"]: m for m in s["masters"] if m["status"] == "assigned"}
        self.assertEqual(skills["pottery"]["master_id"], "pot2")
        self.assertTrue(any(r["code"] == "MASTER_REASSIGNED" for r in s["state_reasons"]))
        replay = core.replay(self.conn, DATE, DATE)
        kinds = [e["type"] for e in replay["reassignments"]]
        self.assertIn("MASTER_LEAVE", kinds)
        self.assertIn("MASTER_REASSIGNED", kinds)

    def test_master_leave_without_backup_is_explainable(self):
        """讲解员只有一名：请假后无人能顶，状态可解释。"""
        bk = make_booking(self.conn, headcount=20)
        confirm_first_plan(self.conn, bk["id"], 20)
        result = core.master_leave(self.conn, "doc1", {
            "start_at": f"{DATE}T08:00:00", "end_at": f"{DATE}T12:00:00"})
        self.assertEqual(len(result["unassigned"]), 1)
        s = core.list_sessions(self.conn, booking_id=bk["id"])[0]
        self.assertTrue(any(r["code"] == "MASTER_UNASSIGNED" for r in s["state_reasons"]))

    def test_cross_day_course(self):
        """跨日课程：两日课在两天各占资源，回放按天呈现。"""
        for i, (start, end) in enumerate([("09:00", "10:30"), ("13:00", "14:30")]):
            core.create_slot(self.conn, {"id": f"yard2_{i}", "venue_id": "yard",
                                         "date": DAY2, "start": start, "end": end,
                                         "capacity": 40})
            core.create_slot(self.conn, {"id": f"hall2_{i}", "venue_id": "hall",
                                         "date": DAY2, "start": start, "end": end,
                                         "capacity": 30})
        bk = make_booking(self.conn, ref="XDAY", headcount=15, course="paint2")
        self.assertEqual(bk["status"], "planned")
        confirm_first_plan(self.conn, bk["id"], 15)
        sessions = core.list_sessions(self.conn, booking_id=bk["id"])
        self.assertEqual(len(sessions), 2, "两日课应生成两个会话")
        days = sorted(s["date"] for s in sessions)
        self.assertEqual(days, [DATE, DAY2])
        for s in sessions:
            self.assertEqual(s["materials"][0]["qty_reserved"], 15)
            self.assertEqual({m["material_id"] for m in s["materials"]}, {"clay", "paint"})
        replay = core.replay(self.conn, DATE, DAY2)
        cap_by_date = {}
        for c in replay["capacity"]:
            cap_by_date.setdefault(c["date"], 0)
            cap_by_date[c["date"]] += c["used"]
        self.assertEqual(cap_by_date[DATE], 15)
        self.assertEqual(cap_by_date[DAY2], 15)

    def test_settlement_basis(self):
        """结算依据：按已完成会话汇总人数、材料消耗与师傅工时。"""
        bk = make_booking(self.conn, headcount=20)
        confirm_first_plan(self.conn, bk["id"], 20)
        s = core.list_sessions(self.conn, booking_id=bk["id"])[0]
        core.start_session(self.conn, s["id"])
        core.complete_session(self.conn, s["id"], {"attended": 18})
        st = core.settlement(self.conn, DATE, DATE)
        self.assertEqual(st["totals"]["sessions"], 1)
        self.assertEqual(st["totals"]["attended"], 18)
        self.assertEqual(st["totals"]["materials"]["clay"]["qty"], 18)
        hours = st["totals"]["master_hours"]
        self.assertEqual(hours["doc1"]["hours"], 1.5)
        self.assertEqual(hours["pot1"]["hours"], 1.5)
        line = st["lines"][0]
        self.assertEqual(line["org_name"], "育才小学")
        self.assertEqual(line["materials"][0]["qty_consumed"], 18)
        # 回放内嵌结算，复盘时口径一致
        replay = core.replay(self.conn, DATE, DATE)
        self.assertEqual(replay["settlement"]["totals"], st["totals"])


if __name__ == "__main__":
    unittest.main()
