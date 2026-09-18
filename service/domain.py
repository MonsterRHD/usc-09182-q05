"""研学体验容量管家 —— 领域模型与状态。

状态只能通过 service.app 中的事件折叠(replay)得到，
本模块只提供数据结构、常量和无副作用的查询。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

# ---- 全局常量 -------------------------------------------------------------

SLOTS_PER_DAY = 4
SLOT_LABELS = ("上午一场", "上午二场", "下午一场", "下午二场")
PLAN_HORIZON_DAYS = 7

GUIDE_SKILL = "GUIDE"  # 讲解员技能码

VENUE_ANY = "ANY"
VENUE_INDOOR = "INDOOR"
VENUE_OUTDOOR = "OUTDOOR"
VENUE_KINDS = (VENUE_ANY, VENUE_INDOOR, VENUE_OUTDOOR)

# ---- 会话状态 -------------------------------------------------------------

ST_CONFIRMED = "CONFIRMED"          # 已确认，资源为软预留
ST_LATE = "LATE"                    # 团队迟到，软预留继续保留
ST_IN_PROGRESS = "IN_PROGRESS"      # 已开始，资源硬锁定
ST_COMPLETED = "COMPLETED"          # 正常结束，已结算
ST_CANCELLED = "CANCELLED"          # 开始前取消，预留已释放
ST_TERMINATED = "TERMINATED"        # 开始后提前终止，按实结算
ST_SPLIT = "SPLIT"                  # 已拆团，父团终结

ACTIVE_STATUSES = (ST_CONFIRMED, ST_LATE, ST_IN_PROGRESS)
TERMINAL_STATUSES = (ST_COMPLETED, ST_CANCELLED, ST_TERMINATED, ST_SPLIT)

# 可解释异常码
INC_STAFF_SHORTAGE = "STAFF_SHORTAGE"            # 无可用师傅/讲解
INC_RAIN_RELOCATED = "RAIN_RELOCATED"            # 雨天已转室
INC_RAIN_NO_ROOM = "RAIN_NO_INDOOR_CAPACITY"     # 雨天室内容量不足
INC_RAIN_PROTECTED = "RAIN_STARTED_PROTECTED"    # 已开始，雨天不挪场
INC_MATERIAL_SHORT = "MATERIAL_SHORTAGE"         # 材料不足（含部分满足）
INC_PARTIAL_KITS = "PARTIAL_KITS_AT_START"       # 开场时材料仅部分满足

# 方案不可行原因码
R_VENUE_FULL = "VENUE_FULL"
R_NO_GUIDE = "NO_GUIDE"
R_NO_MAKER = "NO_MAKER"
R_MATERIAL = "MATERIAL_SHORT"


# ---- 主数据 ---------------------------------------------------------------

@dataclass(frozen=True)
class CourseVersion:
    """课程版本（不可变）；新做法/新材料以新版本登记。"""
    course_code: str
    version: int
    title: str
    maker_skill: str                       # 制坯所需师傅技能码
    material_sku: str                      # 默认材料包
    guides: int = 1                        # 每场讲解员数量
    per_maker_ratio: int = 6               # 每多少名学生配一名制坯师
    max_group_size: int = 30               # 单组人数上限
    venue_kind: str = VENUE_ANY            # 场地要求
    alt_skus: tuple[str, ...] = ()         # 可替代材料包
    # 相对 (起始日, 起始时段) 的占用模式：(日偏移, 时段偏移)；跨日课程形如 ((0,0),(1,0))
    pattern: tuple[tuple[int, int], ...] = ((0, 0),)
    unit_fee_cents: int = 0                # 每人结算单价（分）


@dataclass(frozen=True)
class Venue:
    venue_id: str
    name: str
    indoor: bool
    capacity: int


@dataclass
class Master:
    master_id: str
    name: str
    skills: frozenset[str]
    # (date_iso, slot) 集合，含已登记的请假/不可用
    leaves: set[tuple[str, int]] = field(default_factory=set)


@dataclass
class StockItem:
    """材料包台账：总入库 + 调整 - 预留 - 已消耗 = 可用。"""
    received: int = 0
    adjustments: int = 0
    holds: dict[str, int] = field(default_factory=dict)   # session_id -> 预留数
    consumed: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.received + self.adjustments

    @property
    def held(self) -> int:
        return sum(self.holds.values())

    @property
    def used(self) -> int:
        return sum(self.consumed.values())

    @property
    def free(self) -> int:
        return self.total - self.held - self.used


@dataclass
class RosterEntry:
    """参与名单的最小信息：只需学生编号，不存姓名等隐私。"""
    code: str
    attended: bool = False


@dataclass
class Session:
    session_id: str
    booking_id: str
    plan_id: str
    option_id: str
    course_code: str
    version: int
    group_no: int
    size: int
    slots: list[tuple[str, int]]
    # 每个占用时段独立记录场地/师傅，跨日课程与雨天转室可逐日调整
    venue_by_slot: dict[str, str]
    staff_by_slot: dict[str, dict[str, str]]   # slot_key -> {技能码: 师傅id}
    material_sku: str
    held: int
    roster: dict[str, RosterEntry]
    status: str = ST_CONFIRMED
    parent_id: str | None = None
    child_ids: list[str] = field(default_factory=list)
    confirm_order: int = 0
    settlement: dict | None = None

    @property
    def attended_count(self) -> int:
        return sum(1 for e in self.roster.values() if e.attended)


@dataclass
class State:
    courses: dict[str, dict[int, CourseVersion]] = field(default_factory=dict)
    venues: dict[str, Venue] = field(default_factory=dict)
    masters: dict[str, Master] = field(default_factory=dict)
    stock: dict[str, StockItem] = field(default_factory=dict)
    bookings: dict[str, dict] = field(default_factory=dict)
    plans: dict[str, dict] = field(default_factory=dict)
    sessions: dict[str, Session] = field(default_factory=dict)
    receipts: dict[str, dict] = field(default_factory=dict)
    incidents: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    next_order: int = 0

    # --- 便捷查询 ---

    def course(self, code: str, version: int | None = None) -> CourseVersion:
        versions = self.courses[code]
        if version is None:
            version = max(versions)
        return versions[version]

    def stock_item(self, sku: str) -> StockItem:
        return self.stock.setdefault(sku, StockItem())

    def active_sessions(self) -> list[Session]:
        return [s for s in self.sessions.values() if s.status in ACTIVE_STATUSES]

    def sessions_on(self, day: str) -> list[Session]:
        return [s for s in self.sessions.values()
                if any(d == day for d, _ in s.slots)]


def slot_key(d: str, slot: int) -> str:
    return f"{d}|{slot}"


def expand_slots(start_date: str, start_slot: int,
                 pattern: tuple[tuple[int, int], ...]) -> list[tuple[str, int]]:
    """把课程占用模式展开为具体 (日期, 时段) 列表。"""
    base = date.fromisoformat(start_date)
    out: list[tuple[str, int]] = []
    for day_off, slot_off in pattern:
        d = (base + timedelta(days=day_off)).isoformat()
        s = start_slot + slot_off
        if not (0 <= s < SLOTS_PER_DAY):
            raise ValueError(f"时段越界: {s}")
        out.append((d, s))
    return out
