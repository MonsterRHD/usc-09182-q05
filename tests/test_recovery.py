"""系统恢复：服务重启后，当天未结束的课程仍可交接。"""

import os
import tempfile
import unittest

from service import core
from service.db import Store
from tests.helpers import DATE, confirm_first_plan, make_booking, seed_master_data


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "app.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_handover_survives_restart(self):
        # 第一次启动：建单、确认、开始一组
        store = Store(self.db_path)
        seed_master_data(store.conn)
        bk = make_booking(store.conn, headcount=35)
        confirm_first_plan(store.conn, bk["id"], 35)
        sessions = core.list_sessions(store.conn, date=DATE)
        self.assertEqual(len(sessions), 2)
        core.start_session(store.conn, sessions[0]["id"])
        core.report_late(store.conn, sessions[1]["id"], {"minutes": 15})
        store.conn.commit()
        store.close()  # 模拟服务停止

        # 第二次启动：交接视图必须完整呈现当天未结束的会话
        store2 = Store(self.db_path)
        handover = core.handover(store2.conn, DATE)
        self.assertEqual(handover["summary"]["open"], 2)
        self.assertEqual(handover["summary"]["in_progress"], 1)
        self.assertEqual(handover["summary"]["confirmed"], 1)
        by_status = {s["status"]: s for s in handover["open_sessions"]}
        started = by_status["in_progress"]
        self.assertTrue(started["locked"])
        self.assertTrue(any(r["code"] == "RESOURCE_LOCKED" for r in started["state_reasons"]))
        self.assertEqual(len(started["masters"]), 2, "交接要能看到带组师傅")
        late = by_status["confirmed"]
        self.assertTrue(any(r["code"] == "LATE_ARRIVAL" for r in late["state_reasons"]),
                        "交接要能看到迟到待办")
        # 恢复后业务可继续：完结已开始的会话
        done = core.complete_session(store2.conn, started["id"], {"attended": 18})
        self.assertEqual(done["status"], "completed")
        store2.close()

    def test_events_survive_restart_for_replay(self):
        store = Store(self.db_path)
        seed_master_data(store.conn)
        bk = make_booking(store.conn, headcount=20)
        confirm_first_plan(store.conn, bk["id"], 20)
        store.conn.commit()
        store.close()
        store2 = Store(self.db_path)
        replay = core.replay(store2.conn, DATE, DATE)
        types = [e["type"] for e in replay["events"]]
        self.assertIn("BOOKING_RECEIVED", types)
        self.assertIn("BOOKING_CONFIRMED", types)
        self.assertIn("ROSTER_SAVED", types)
        store2.close()


if __name__ == "__main__":
    unittest.main()
