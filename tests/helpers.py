"""测试共享设施：内存库 fixtures 与真实 HTTP 客户端。"""

import http.client
import json
import threading
import unittest

from service import core
from service.api import make_server
from service.db import Store

DATE = "2026-10-01"  # 中秋国庆双节高峰第一天


def seed_master_data(conn):
    """展示馆（室内）+ 大师工坊（室外），讲解员/制坯师，陶土包与彩绘包。"""
    core.create_venue(conn, {"id": "hall", "name": "展示馆", "kind": "indoor"})
    core.create_venue(conn, {"id": "yard", "name": "大师工坊", "kind": "outdoor"})
    core.create_master(conn, {"id": "doc1", "name": "讲解员小刘", "skills": ["docent"]})
    core.create_master(conn, {"id": "pot1", "name": "制坯师老王", "skills": ["pottery"]})
    core.create_master(conn, {"id": "pot2", "name": "制坯师老赵", "skills": ["pottery"]})
    core.upsert_material(conn, {"id": "clay", "name": "陶土包", "stock": 60})
    core.upsert_material(conn, {"id": "paint", "name": "彩绘包", "stock": 40})
    core.create_course(conn, {
        "id": "pottery1", "name": "泥塑体验", "version": "1.0", "duration_min": 90,
        "group_size_max": 20, "venue_kind": "outdoor",
        "skill_reqs": {"docent": 1, "pottery": 1}, "material_reqs": {"clay": 1}})
    core.create_course(conn, {
        "id": "paint2", "name": "泥塑彩绘两日课", "version": "2.1", "duration_min": 90,
        "days": 2, "group_size_max": 15, "venue_kind": "any",
        "skill_reqs": {"docent": 1}, "material_reqs": {"clay": 1, "paint": 1}})
    for i, (start, end) in enumerate([("09:00", "11:00"), ("11:00", "13:00"),
                                      ("13:00", "15:00"), ("15:00", "17:00")]):
        core.create_slot(conn, {"id": f"yard{i}", "venue_id": "yard", "date": DATE,
                                "start": start, "end": end, "capacity": 40})
        core.create_slot(conn, {"id": f"hall{i}", "venue_id": "hall", "date": DATE,
                                "start": start, "end": end, "capacity": 30})


def make_booking(conn, ref="R1", headcount=35, course="pottery1", date=DATE,
                 window=("08:00", "18:00"), channel="wechat"):
    bk, _ = core.create_booking(conn, {
        "channel": channel, "external_ref": ref, "org_name": "育才小学",
        "headcount": headcount, "course_id": course, "desired_date": date,
        "window_start": window[0], "window_end": window[1]})
    return bk


def confirm_first_plan(conn, booking_id, headcount):
    return core.confirm_booking(conn, booking_id, {"roster": {"headcount": headcount}})


class ApiTestCase(unittest.TestCase):
    """每个用例一套独立内存库 + 随机端口真实 HTTP 服务。"""

    def setUp(self):
        self.store = Store(":memory:")
        seed_master_data(self.store.conn)
        self.server = make_server(self.store, host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        return resp.status, data

    def post(self, path, body=None, headers=None):
        return self.call("POST", path, body, headers)

    def get(self, path):
        return self.call("GET", path)
