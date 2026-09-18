"""研学体验容量管家 —— HTTP 入口（仅标准库）。

写接口建议带 Idempotency-Key 请求头；同键重试返回首次结果，不重复产生事件。
事件默认追加到 data/events.jsonl，重启即回放恢复，可用 EVENT_FILE 覆盖路径。
"""
from __future__ import annotations

import json
import os
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from .app import CapacityService, ServiceError
from .store import EventStore


def _build_service() -> CapacityService:
    path = os.getenv("EVENT_FILE", os.path.join("data", "events.jsonl"))
    if path and os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    return CapacityService(EventStore(path or None))


SERVICE: CapacityService | None = None


def get_service() -> CapacityService:
    global SERVICE
    if SERVICE is None:
        SERVICE = _build_service()
    return SERVICE


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=list).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ServiceError("BAD_JSON", f"请求体不是合法 JSON: {exc}")
        if not isinstance(data, dict):
            raise ServiceError("BAD_JSON", "请求体必须是 JSON 对象")
        return data

    def log_message(self, *_args) -> None:
        pass

    # ---- GET ----

    def do_GET(self) -> None:  # noqa: N802
        svc = get_service()
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        try:
            if path == "/health":
                self._send(200, {"status": "ok"})
            elif path.startswith("/plans/"):
                self._send(200, svc.plan_view(path.rsplit("/", 1)[-1]))
            elif path.startswith("/sessions/"):
                sid = path.split("/")[2]
                if path.endswith("/incidents"):
                    self._send(200, {"incidents": svc.incidents(sid)})
                else:
                    self._send(200, svc.session_view(sid))
            elif path == "/schedule":
                self._send(200, svc.day_schedule(qs["date"][0]))
            elif path == "/stock":
                self._send(200, svc.stock_view())
            elif path == "/incidents":
                self._send(200, {"incidents": svc.incidents()})
            elif path == "/handover":
                self._send(200, svc.handover(qs["date"][0]))
            elif path == "/replay":
                seq = int(qs["until_seq"][0])
                st = svc.replay_until(seq)
                self._send(200, {
                    "until_seq": seq,
                    "sessions": {sid: s.status for sid, s in st.sessions.items()},
                    "stock": {sku: {"total": it.total, "held": it.held,
                                    "consumed": it.used, "free": it.free}
                              for sku, it in st.stock.items()},
                    "incidents": list(st.incidents)})
            else:
                self._send(404, {"error": "NOT_FOUND", "message": path})
        except ServiceError as exc:
            self._send(404 if "NOT_FOUND" in exc.code else 400,
                       {"error": exc.code, "message": exc.message, "details": exc.details})
        except KeyError as exc:
            self._send(404, {"error": "NOT_FOUND", "message": str(exc).strip("'\"")})

    # ---- POST ----

    def do_POST(self) -> None:  # noqa: N802
        svc = get_service()
        path = urlparse(self.path).path
        key = self.headers.get("Idempotency-Key") or f"auto-{uuid.uuid4()}"
        try:
            body = self._read_json()
            result, status = self._dispatch(svc, key, path, body), 200
        except ServiceError as exc:
            result = {"error": exc.code, "message": exc.message, "details": exc.details}
            status = 409 if exc.code in ("PLAN_NOT_FEASIBLE", "SPLIT_NOT_FEASIBLE") else 400
        self._send(status, result)

    def _dispatch(self, svc: CapacityService, key: str, path: str, b: dict) -> dict:
        if path == "/admin/courses":
            return svc.register_course(
                key, b["course_code"], int(b["version"]), b["title"],
                b["maker_skill"], b["material_sku"],
                guides=int(b.get("guides", 1)),
                per_maker_ratio=int(b.get("per_maker_ratio", 6)),
                max_group_size=int(b.get("max_group_size", 30)),
                venue_kind=b.get("venue_kind", "ANY"),
                alt_skus=tuple(b.get("alt_skus", [])),
                pattern=tuple(tuple(p) for p in b.get("pattern", ((0, 0),))),
                unit_fee_cents=int(b.get("unit_fee_cents", 0)))
        if path == "/admin/venues":
            return svc.register_venue(key, b["venue_id"], b["name"],
                                      bool(b["indoor"]), int(b["capacity"]))
        if path == "/admin/masters":
            return svc.register_master(key, b["master_id"], b["name"], b["skills"])
        if path == "/admin/leaves":
            return svc.register_leave(key, b["master_id"], b["date"],
                                      int(b["slot"]), b.get("reason", ""))
        if path == "/admin/leaves/cancel":
            return svc.cancel_leave(key, b["master_id"], b["date"], int(b["slot"]))
        if path == "/admin/stock/receive":
            return svc.receive_stock(key, b["sku"], int(b["qty"]))
        if path == "/admin/stock/adjust":
            return svc.adjust_stock(key, b["sku"], int(b["delta"]), b.get("reason", ""))

        if path == "/teams":
            return svc.receive_team(
                key, b.get("booking_id"), b["course_code"],
                b["start_date"], int(b["start_slot"]), int(b["students"]),
                channel=b.get("channel", "onsite"), note=b.get("note", ""),
                version_pref=b.get("version_pref"),
                rain_indoor=bool(b.get("rain_indoor", False)))

        if path.endswith("/confirm"):
            plan_id = path.split("/")[2]
            return svc.confirm_plan(key, plan_id, b.get("roster_by_option"))
        if path.endswith("/roster"):
            return svc.save_roster(key, path.split("/")[2], b["student_codes"])
        if path.endswith("/late"):
            return svc.mark_late(key, path.split("/")[2], b.get("note", ""))
        if path.endswith("/start"):
            return svc.start_session(key, path.split("/")[2], b.get("attended_codes"))
        if path.endswith("/complete"):
            return svc.complete_session(key, path.split("/")[2])
        if path.endswith("/cancel"):
            return svc.cancel_session(key, path.split("/")[2], b.get("reason", ""))
        if path.endswith("/terminate"):
            return svc.terminate_session(key, path.split("/")[2], b.get("reason", ""))
        if path.endswith("/split"):
            return svc.split_team(key, path.split("/")[2], b["groups"])
        if path == "/weather/rain":
            return svc.rain_switch(key, b["date"], int(b["slot"]))

        raise ServiceError("NOT_FOUND", f"未知路径: {path}")


def run() -> None:
    HTTPServer(("0.0.0.0", int(os.getenv("PORT", "8000"))), Handler).serve_forever()


if __name__ == "__main__":
    run()
