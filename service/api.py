"""HTTP 层：REST 路由、JSON 编解码、统一错误格式、Idempotency-Key 通用幂等。"""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import core
from .errors import ApiError, bad_request
from .util import digest_of, dumps, loads, now_iso


def _q(query, name):
    m = re.search(rf"(?:^|&){name}=([^&]*)", query or "")
    return m.group(1) if m else None


# 路由表：(方法, 路径正则, 处理器)。处理器签名 (store, match, body, query) -> (status, obj)
ROUTES = [
    ("GET", r"^/health$", lambda s, m, b, q: (200, {"status": "ok"})),
    # 主数据
    ("POST", r"^/courses$", lambda s, m, b, q: (201, core.create_course(s.conn, b))),
    ("GET", r"^/courses$", lambda s, m, b, q: (200, {"items": core.list_courses(s.conn)})),
    ("POST", r"^/venues$", lambda s, m, b, q: (201, core.create_venue(s.conn, b))),
    ("GET", r"^/venues$", lambda s, m, b, q: (200, {"items": core.list_venues(s.conn)})),
    ("POST", r"^/slots$", lambda s, m, b, q: (201, core.create_slot(s.conn, b))),
    ("GET", r"^/slots$", lambda s, m, b, q: (200, {"items": core.list_slots(
        s.conn, date=_q(q, "date"), venue_id=_q(q, "venue_id"))})),
    ("POST", r"^/masters$", lambda s, m, b, q: (201, core.create_master(s.conn, b))),
    ("GET", r"^/masters$", lambda s, m, b, q: (200, {"items": core.list_masters(s.conn)})),
    ("POST", r"^/masters/([^/]+)/leave$",
     lambda s, m, b, q: (200, core.master_leave(s.conn, m.group(1), b))),
    ("POST", r"^/materials$", lambda s, m, b, q: (201, core.upsert_material(s.conn, b))),
    ("GET", r"^/materials$", lambda s, m, b, q: (200, {"items": core.list_materials(s.conn)})),
    ("POST", r"^/materials/([^/]+)/adjust$",
     lambda s, m, b, q: (200, core.adjust_material(s.conn, m.group(1), b))),
    # 团队需求与方案
    ("POST", r"^/bookings$", lambda s, m, b, q: _create_booking(s, b)),
    ("GET", r"^/bookings$", lambda s, m, b, q: (200, {"items": core.list_bookings(
        s.conn, date=_q(q, "date"), status=_q(q, "status"))})),
    ("GET", r"^/bookings/([^/]+)$",
     lambda s, m, b, q: (200, core.get_booking(s.conn, m.group(1)))),
    ("POST", r"^/bookings/([^/]+)/confirm$",
     lambda s, m, b, q: (200, core.confirm_booking(s.conn, m.group(1), b))),
    ("POST", r"^/bookings/([^/]+)/cancel$",
     lambda s, m, b, q: (200, core.cancel_booking(s.conn, m.group(1), b))),
    ("POST", r"^/bookings/([^/]+)/roster$",
     lambda s, m, b, q: (200, core.save_roster(s.conn, m.group(1), b,
                                               occurred_at=(b or {}).get("occurred_at")))),
    # 会话执行与现场事件
    ("GET", r"^/sessions$", lambda s, m, b, q: (200, {"items": core.list_sessions(
        s.conn, date=_q(q, "date"), status=_q(q, "status"), booking_id=_q(q, "booking_id"))})),
    ("GET", r"^/sessions/([^/]+)$",
     lambda s, m, b, q: (200, core.get_session(s.conn, m.group(1)))),
    ("POST", r"^/sessions/([^/]+)/start$",
     lambda s, m, b, q: (200, core.start_session(s.conn, m.group(1)))),
    ("POST", r"^/sessions/([^/]+)/complete$",
     lambda s, m, b, q: (200, core.complete_session(s.conn, m.group(1), b))),
    ("POST", r"^/sessions/([^/]+)/late$",
     lambda s, m, b, q: (200, core.report_late(s.conn, m.group(1), b))),
    ("POST", r"^/sessions/([^/]+)/split$",
     lambda s, m, b, q: (200, core.split_session(s.conn, m.group(1), b))),
    ("POST", r"^/sessions/([^/]+)/transfer$",
     lambda s, m, b, q: (200, core.transfer_session(s.conn, m.group(1), b))),
    # 交接 / 回放 / 结算
    ("GET", r"^/handover$",
     lambda s, m, b, q: (200, core.handover(s.conn, _q(q, "date")))),
    ("GET", r"^/replay$", lambda s, m, b, q: (200, core.replay(
        s.conn, _q(q, "from") or "", _q(q, "to") or ""))),
    ("GET", r"^/settlement$", lambda s, m, b, q: (200, core.settlement(
        s.conn, _q(q, "from") or "", _q(q, "to") or ""))),
]


def _create_booking(store, body):
    result, replayed = core.create_booking(store.conn, body,
                                           occurred_at=(body or {}).get("occurred_at"))
    return (200 if replayed else 201), result


def dispatch(store, method, path, query, body, idem_key=None):
    for mth, pattern, handler in ROUTES:
        if mth != method:
            continue
        m = re.match(pattern, path)
        if m:
            return handler(store, m, body, query)
    raise ApiError(404, "NOT_FOUND", f"路径不存在：{method} {path}")


class Handler(BaseHTTPRequestHandler):
    store = None  # 由 make_server 注入

    protocol_version = "HTTP/1.1"

    def _handle(self, method):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = None
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    raise bad_request("INVALID_JSON", "请求体不是合法 JSON")
                if not isinstance(body, (dict, list)):
                    raise bad_request("INVALID_JSON", "请求体必须是 JSON 对象")
            path, _, query = self.path.partition("?")
            idem_key = self.headers.get("Idempotency-Key")
            with self.store.lock:
                status, obj = self._dispatch_idempotent(method, path, query, body, idem_key)
                self.store.conn.commit()
            self._send(status, obj)
        except ApiError as e:
            self.store.conn.rollback()
            self._send(e.status, e.to_dict())
        except Exception as e:  # 兜底，避免连接悬挂
            self.store.conn.rollback()
            self._send(500, {"error": {"code": "INTERNAL", "message": str(e), "details": {}}})

    def _dispatch_idempotent(self, method, path, query, body, idem_key):
        """通用幂等：带 Idempotency-Key 的写请求，重试时直接返回首次结果。"""
        if method != "POST" or not idem_key:
            return dispatch(self.store, method, path, query, body)
        row = self.store.conn.execute("SELECT * FROM idempotency_keys WHERE key=?",
                                      (idem_key,)).fetchone()
        req_hash = digest_of({"method": method, "path": path, "body": body})
        if row:
            if row["request_hash"] != req_hash:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT",
                               "相同 Idempotency-Key 携带了不同的请求体",
                               {"key": idem_key})
            return 200, {**loads(row["response"]), "_idempotent_replay": True}
        status, obj = dispatch(self.store, method, path, query, body)
        if status < 500:
            self.store.conn.execute(
                "INSERT OR IGNORE INTO idempotency_keys(key,endpoint,request_hash,response,"
                "created_at) VALUES(?,?,?,?,?)",
                (idem_key, f"{method} {path}", req_hash, dumps(obj), now_iso()))
        return status, obj

    def _send(self, status, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def log_message(self, *_):
        pass


def make_server(store, host="0.0.0.0", port=8000):
    handler = type("BoundHandler", (Handler,), {"store": store})
    return ThreadingHTTPServer((host, port), handler)
