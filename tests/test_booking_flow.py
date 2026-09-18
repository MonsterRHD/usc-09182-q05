"""团队需求 → 分组与替代方案 → 确认锁资源 的主流程。"""

import unittest

from service import core
from service.db import Store
from tests.helpers import DATE, confirm_first_plan, make_booking, seed_master_data


class BookingFlowTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.conn = self.store.conn
        seed_master_data(self.conn)

    def tearDown(self):
        self.store.close()

    def test_group_split_and_primary_plan(self):
        """35 人超出单组上限 20，应拆成 18+17 两组并给出首选方案。"""
        bk = make_booking(self.conn, headcount=35)
        self.assertEqual(bk["status"], "planned")
        primary = [p for p in bk["plans"] if p["kind"] == "primary"]
        self.assertEqual(len(primary), 1)
        groups = primary[0]["groups"]
        self.assertEqual(sorted(g["headcount"] for g in groups), [17, 18])
        split_reason = [r for r in primary[0]["explanation"] if r["code"] == "SPLIT_GROUP"]
        self.assertTrue(split_reason, "拆团应产生可解释状态")
        for g in groups:
            self.assertEqual(len(g["masters"]), 2)  # 讲解员 + 制坯师
            self.assertEqual(g["materials"], {"clay": g["headcount"]})

    def test_alternative_plan_when_window_infeasible(self):
        """时间窗内师傅不够时，首选失败、给出放宽时间窗的替代方案。"""
        bk = make_booking(self.conn, headcount=35, window=("08:00", "12:00"))
        kinds = [p["kind"] for p in bk["plans"]]
        # 上午窗内讲解员只有 1 名，两组无法同时带 → 首选不可行
        self.assertNotIn("primary", kinds)
        self.assertIn("alternative", kinds)
        alt = bk["plans"][0]
        self.assertEqual(alt["explanation"][0]["code"], "ALTERNATIVE_ADJUSTMENT")

    def test_needs_attention_when_no_capacity(self):
        """全部时段都装不下时，需求进入 needs_attention 并带原因。"""
        bk = make_booking(self.conn, ref="BIG", headcount=500)
        self.assertEqual(bk["status"], "needs_attention")
        self.assertTrue(bk["state_reasons"])
        self.assertEqual(bk["plans"], [])

    def test_confirm_reserves_resources(self):
        """确认后：会话落库、容量被占、材料被预留、师傅被指派。"""
        bk = make_booking(self.conn, headcount=20)
        out = confirm_first_plan(self.conn, bk["id"], 20)
        self.assertEqual(out["status"], "confirmed")
        sessions = core.list_sessions(self.conn, booking_id=bk["id"])
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s["status"], "confirmed")
        self.assertEqual(len(s["masters"]), 2)
        slots = {sl["id"]: sl for sl in core.list_slots(self.conn, date=DATE)}
        self.assertEqual(slots[s["slot_id"]]["used"], 20)
        mats = {m["id"]: m for m in core.list_materials(self.conn)}
        self.assertEqual(mats["clay"]["reserved"], 20)
        self.assertEqual(mats["clay"]["available"], 40)

    def test_confirm_requires_roster_minimal(self):
        """每次确认必须保存参与名单最小信息。"""
        bk = make_booking(self.conn, headcount=20)
        with self.assertRaises(core.ApiError) as ctx:
            core.confirm_booking(self.conn, bk["id"], {})
        self.assertEqual(ctx.exception.code, "ROSTER_REQUIRED")
        with self.assertRaises(core.ApiError) as ctx:
            core.confirm_booking(self.conn, bk["id"], {"roster": {"headcount": 19}})
        self.assertEqual(ctx.exception.code, "ROSTER_MISMATCH")
        out = core.confirm_booking(self.conn, bk["id"], {
            "roster": {"headcount": 20, "age_band": "小学高年级", "roster_ref": "ch-8848"}})
        self.assertEqual(len(out["rosters"]), 1)
        r = out["rosters"][0]
        self.assertEqual(r["headcount"], 20)
        self.assertEqual(r["age_band"], "小学高年级")
        self.assertTrue(r["digest"])
        # 最小信息：不出现姓名/证件等个人字段
        self.assertNotIn("names", r)
        self.assertNotIn("id_cards", r)

    def test_second_booking_cannot_overbook_slot(self):
        """容量被确认占用后，后来的需求只能去别的时段。"""
        bk1 = make_booking(self.conn, ref="R1", headcount=20)
        confirm_first_plan(self.conn, bk1["id"], 20)
        bk2 = make_booking(self.conn, ref="R2", headcount=35, window=("08:00", "12:00"))
        self.assertEqual(bk2["status"], "planned")
        used_slot_ids = {g["slot_id"] for p in bk2["plans"] for g in p["groups"]}
        s1 = core.list_sessions(self.conn, booking_id=bk1["id"])[0]
        # 第一单占了 09:00 的 20 人，第二单若同时段只剩 20 容量，装不下 18 人组时会错开
        for p in bk2["plans"]:
            for g in p["groups"]:
                slot = next(sl for sl in core.list_slots(self.conn, date=DATE)
                            if sl["id"] == g["slot_id"])
                self.assertGreaterEqual(slot["capacity"] - slot["used"], g["headcount"])
        self.assertTrue(used_slot_ids)

    def test_plan_stale_when_material_changed(self):
        """方案生成后材料被别的确认占掉，确认时应整体失败并解释。"""
        bk1 = make_booking(self.conn, ref="R1", headcount=20)
        bk2 = make_booking(self.conn, ref="R2", headcount=20)
        confirm_first_plan(self.conn, bk1["id"], 20)
        # 把库存调到只够 bk1，bk2 的方案即过期
        core.adjust_material(self.conn, "clay", {"delta": -40, "reason": "盘点修正"})
        with self.assertRaises(core.ApiError) as ctx:
            confirm_first_plan(self.conn, bk2["id"], 20)
        self.assertEqual(ctx.exception.code, "PLAN_STALE")
        self.assertTrue(ctx.exception.details["reasons"])


if __name__ == "__main__":
    unittest.main()
