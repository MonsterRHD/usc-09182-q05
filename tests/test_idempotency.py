"""幂等：渠道重试、Idempotency-Key、离线补录。"""

import unittest

from tests.helpers import ApiTestCase, DATE


class IdempotencyTest(ApiTestCase):
    def _booking_body(self, ref="R1"):
        return {"channel": "wechat", "external_ref": ref, "org_name": "育才小学",
                "headcount": 20, "course_id": "pottery1", "desired_date": DATE,
                "window_start": "08:00", "window_end": "18:00"}

    def test_channel_retry_returns_same_booking(self):
        """渠道重试（相同 channel+external_ref）返回同一单据，不重复建单。"""
        s1, b1 = self.post("/bookings", self._booking_body())
        self.assertEqual(s1, 201)
        s2, b2 = self.post("/bookings", self._booking_body())
        self.assertEqual(s2, 200)
        self.assertEqual(b1["id"], b2["id"])
        s, lst = self.get("/bookings")
        self.assertEqual(len(lst["items"]), 1)

    def test_idempotency_key_replays_response(self):
        """通用 Idempotency-Key：重试返回首次响应，副作用只发生一次。"""
        headers = {"Idempotency-Key": "confirm-abc"}
        bk_status, bk = self.post("/bookings", self._booking_body())
        body = {"roster": {"headcount": 20}}
        s1, r1 = self.post(f"/bookings/{bk['id']}/confirm", body, headers)
        self.assertEqual(s1, 200)
        s2, r2 = self.post(f"/bookings/{bk['id']}/confirm", body, headers)
        self.assertEqual(s2, 200)
        self.assertTrue(r2.get("_idempotent_replay"))
        # 材料只预留了一次
        s, mats = self.get("/materials")
        clay = [m for m in mats["items"] if m["id"] == "clay"][0]
        self.assertEqual(clay["reserved"], 20)

    def test_idempotency_key_conflict_on_different_body(self):
        s, bk = self.post("/bookings", self._booking_body())
        headers = {"Idempotency-Key": "same-key"}
        self.post(f"/bookings/{bk['id']}/confirm",
                  {"roster": {"headcount": 20}}, headers)
        s2, r2 = self.post(f"/bookings/{bk['id']}/cancel", {"reason": "x"}, headers)
        self.assertEqual(s2, 409)
        self.assertEqual(r2["error"]["code"], "IDEMPOTENCY_CONFLICT")

    def test_offline_roster_backfill_idempotent(self):
        """离线补录名单：相同 idem_key 重复提交只记一次。"""
        s, bk = self.post("/bookings", self._booking_body())
        payload = {"idem_key": "offline-1", "headcount": 20, "age_band": "小学",
                   "occurred_at": f"{DATE}T07:50:00"}
        s1, r1 = self.post(f"/bookings/{bk['id']}/roster", payload)
        self.assertEqual(s1, 200)
        self.assertEqual(len(r1["rosters"]), 1)
        s2, r2 = self.post(f"/bookings/{bk['id']}/roster", payload)
        self.assertEqual(len(r2["rosters"]), 1, "重复补录不应产生第二条名单")

    def test_roster_headcount_must_match(self):
        s, bk = self.post("/bookings", self._booking_body())
        s2, r2 = self.post(f"/bookings/{bk['id']}/roster",
                           {"idem_key": "k1", "headcount": 21})
        self.assertEqual(s2, 400)
        self.assertEqual(r2["error"]["code"], "ROSTER_MISMATCH")

    def test_offline_booking_backfill_idempotent(self):
        """离线补录建单：带历史发生时间，重试仍按渠道单号判重。"""
        body = {**self._booking_body("OFF-1"), "occurred_at": f"{DATE}T06:30:00"}
        s1, b1 = self.post("/bookings", body)
        self.assertEqual(s1, 201)
        self.assertEqual(b1["created_at"], f"{DATE}T06:30:00")
        s2, b2 = self.post("/bookings", body)
        self.assertEqual(s2, 200)
        self.assertEqual(b1["id"], b2["id"])

    def test_double_confirm_is_idempotent(self):
        """重复确认返回当前状态，不产生第二组会话。"""
        s, bk = self.post("/bookings", self._booking_body())
        self.post(f"/bookings/{bk['id']}/confirm", {"roster": {"headcount": 20}})
        s2, r2 = self.post(f"/bookings/{bk['id']}/confirm", {"roster": {"headcount": 20}})
        self.assertEqual(s2, 200)
        s3, sessions = self.get(f"/sessions?booking_id={bk['id']}")
        self.assertEqual(len(sessions["items"]), 1)


if __name__ == "__main__":
    unittest.main()
