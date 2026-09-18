"""核心不变量：已开始的体验不得被后来的排程夺走资源。"""

import unittest

from service import core
from service.db import Store
from tests.helpers import DATE, confirm_first_plan, make_booking, seed_master_data


class LockInvariantTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.conn = self.store.conn
        seed_master_data(self.conn)

    def tearDown(self):
        self.store.close()

    def _started_session(self, ref="R1", headcount=20):
        bk = make_booking(self.conn, ref=ref, headcount=headcount)
        confirm_first_plan(self.conn, bk["id"], headcount)
        s = core.list_sessions(self.conn, booking_id=bk["id"])[0]
        core.start_session(self.conn, s["id"])
        return core.get_session(self.conn, s["id"])

    def test_start_locks_resources(self):
        s = self._started_session()
        self.assertEqual(s["status"], "in_progress")
        self.assertTrue(s["locked"])
        self.assertTrue([r for r in s["state_reasons"] if r["code"] == "RESOURCE_LOCKED"])

    def test_started_session_cannot_be_cancelled(self):
        s = self._started_session()
        with self.assertRaises(core.ApiError) as ctx:
            core.cancel_booking(self.conn, s["booking_id"], {})
        self.assertEqual(ctx.exception.code, "SESSION_LOCKED")

    def test_started_session_cannot_be_split(self):
        s = self._started_session()
        with self.assertRaises(core.ApiError) as ctx:
            core.split_session(self.conn, s["id"], {"parts": [10, 10]})
        self.assertEqual(ctx.exception.code, "SESSION_LOCKED")

    def test_master_leave_rejected_when_overlaps_started_session(self):
        s = self._started_session()
        with self.assertRaises(core.ApiError) as ctx:
            core.master_leave(self.conn, "pot1", {
                "start_at": f"{DATE}T08:00:00", "end_at": f"{DATE}T12:00:00"})
        self.assertEqual(ctx.exception.code, "SESSION_LOCKED")
        # 师傅仍在岗
        after = core.get_session(self.conn, s["id"])
        skills = {m["skill"]: m for m in after["masters"] if m["status"] == "assigned"}
        self.assertEqual(skills["pottery"]["master_id"], "pot1")

    def test_material_shortage_spares_started_session(self):
        """短缺分配优先保住已开始的会话，缺口全部落在未开始的会话上。"""
        bk1 = make_booking(self.conn, ref="R1", headcount=20)
        confirm_first_plan(self.conn, bk1["id"], 20)
        s1 = core.list_sessions(self.conn, booking_id=bk1["id"])[0]
        core.start_session(self.conn, s1["id"])
        bk2 = make_booking(self.conn, ref="R2", headcount=20)
        confirm_first_plan(self.conn, bk2["id"], 20)
        # 库存 60 → 25：已开始会话要 20，未开始会话要 20，总缺口 15
        out = core.adjust_material(self.conn, "clay", {"delta": -35, "reason": "运输损毁"})
        affected = [a["session_id"] for a in out["affected_sessions"]]
        s2 = core.list_sessions(self.conn, booking_id=bk2["id"])[0]
        self.assertEqual(affected, [s2["id"]])  # 只有未开始的会话被标记
        s1_after = core.get_session(self.conn, s1["id"])
        self.assertFalse([r for r in s1_after["state_reasons"]
                          if r["code"] == "MATERIAL_SHORTAGE"])

    def test_later_booking_cannot_steal_started_sessions_slot(self):
        """已开始会话占着的容量，新需求规划时不可超订。"""
        s = self._started_session(headcount=20)
        bk2 = make_booking(self.conn, ref="R2", headcount=25)
        confirm_first_plan(self.conn, bk2["id"], 25)
        s2 = core.list_sessions(self.conn, booking_id=bk2["id"])[0]
        slots = {sl["id"]: sl for sl in core.list_slots(self.conn, date=DATE)}
        for slot in slots.values():
            self.assertLessEqual(slot["used"], slot["capacity"],
                                 f"{slot['id']} 超订")
        self.assertEqual(slots[s["slot_id"]]["used"],
                         20 if s2["slot_id"] != s["slot_id"] else 45)

    def test_complete_consumes_materials_by_attended(self):
        """完结按实到人数消耗材料，释放师傅，形成结算依据。"""
        s = self._started_session(headcount=20)
        out = core.complete_session(self.conn, s["id"], {"attended": 18})
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["attended"], 18)
        mats = {m["id"]: m for m in core.list_materials(self.conn)}
        self.assertEqual(mats["clay"]["stock"], 60 - 18)
        self.assertEqual(mats["clay"]["reserved"], 0)
        self.assertTrue(all(m["status"] == "released" for m in out["masters"]))


if __name__ == "__main__":
    unittest.main()
