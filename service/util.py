"""时间、标识与 JSON 的小工具。时段用「当日分钟数」表示，便于容量比较；事件用 ISO 时间戳。"""

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def today_iso() -> str:
    return date.today().isoformat()


def check_date(value, field="date") -> str:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        from .errors import bad_request

        raise bad_request("INVALID_DATE", f"{field} 必须是 YYYY-MM-DD 格式", {"field": field, "value": value})
    try:
        date.fromisoformat(value)
    except ValueError:
        from .errors import bad_request

        raise bad_request("INVALID_DATE", f"{field} 不是有效日期", {"field": field, "value": value})
    return value


def add_days(date_str: str, days: int) -> str:
    return (date.fromisoformat(date_str) + timedelta(days=days)).isoformat()


def check_hhmm(value, field) -> int:
    """把 'HH:MM' 转成当日分钟数。"""
    if isinstance(value, int) and 0 <= value < 24 * 60:
        return value
    if isinstance(value, str):
        m = re.match(r"^(\d{1,2}):(\d{2})$", value)
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
            if 0 <= h < 24 and 0 <= mi < 60:
                return h * 60 + mi
    from .errors import bad_request

    raise bad_request("INVALID_TIME", f"{field} 必须是 HH:MM", {"field": field, "value": value})


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def session_start_iso(date_str: str, start_min: int) -> str:
    return f"{date_str}T{hhmm(start_min)}:00"


def session_end_iso(date_str: str, end_min: int) -> str:
    return f"{date_str}T{hhmm(end_min)}:00"


def parse_iso(value, field):
    if not isinstance(value, str):
        from .errors import bad_request

        raise bad_request("INVALID_TIMESTAMP", f"{field} 必须是 ISO 时间", {"field": field, "value": value})
    try:
        datetime.fromisoformat(value)
    except ValueError:
        from .errors import bad_request

        raise bad_request("INVALID_TIMESTAMP", f"{field} 不是有效 ISO 时间", {"field": field, "value": value})
    return value


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def loads(text: str):
    return json.loads(text) if text else None


def digest_of(obj) -> str:
    """名单摘要：对规范化 JSON 取哈希，只用于核对，不保存个人信息。"""
    return hashlib.sha256(dumps(obj).encode("utf-8")).hexdigest()
