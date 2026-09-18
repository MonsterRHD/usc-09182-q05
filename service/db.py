"""SQLite 持久化：所有状态落库，服务重启后当天未结束的课程可继续交接。"""

import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  version TEXT NOT NULL,
  duration_min INTEGER NOT NULL,
  days INTEGER NOT NULL DEFAULT 1,
  venue_kind TEXT NOT NULL DEFAULT 'any',
  group_size_max INTEGER NOT NULL,
  skill_reqs TEXT NOT NULL DEFAULT '{}',
  material_reqs TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS venues (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slots (
  id TEXT PRIMARY KEY,
  venue_id TEXT NOT NULL REFERENCES venues(id),
  date TEXT NOT NULL,
  start_min INTEGER NOT NULL,
  end_min INTEGER NOT NULL,
  capacity INTEGER NOT NULL,
  UNIQUE(venue_id, date, start_min, end_min)
);
CREATE TABLE IF NOT EXISTS masters (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  skills TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS master_leaves (
  id TEXT PRIMARY KEY,
  master_id TEXT NOT NULL REFERENCES masters(id),
  start_at TEXT NOT NULL,
  end_at TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS materials (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT '套',
  stock INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
  id TEXT PRIMARY KEY,
  channel TEXT NOT NULL,
  external_ref TEXT NOT NULL,
  org_name TEXT NOT NULL,
  headcount INTEGER NOT NULL,
  course_id TEXT NOT NULL REFERENCES courses(id),
  desired_date TEXT NOT NULL,
  window_start INTEGER NOT NULL,
  window_end INTEGER NOT NULL,
  status TEXT NOT NULL,
  state_reasons TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(channel, external_ref)
);
CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY,
  booking_id TEXT NOT NULL REFERENCES bookings(id),
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  explanation TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_groups (
  id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL REFERENCES plans(id),
  group_no INTEGER NOT NULL,
  day_index INTEGER NOT NULL,
  headcount INTEGER NOT NULL,
  slot_id TEXT NOT NULL,
  venue_id TEXT NOT NULL,
  date TEXT NOT NULL,
  start_min INTEGER NOT NULL,
  end_min INTEGER NOT NULL,
  masters TEXT NOT NULL DEFAULT '[]',
  materials TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  booking_id TEXT NOT NULL REFERENCES bookings(id),
  plan_id TEXT NOT NULL,
  group_no INTEGER NOT NULL,
  day_index INTEGER NOT NULL,
  course_id TEXT NOT NULL,
  venue_id TEXT NOT NULL,
  slot_id TEXT NOT NULL,
  date TEXT NOT NULL,
  start_min INTEGER NOT NULL,
  end_min INTEGER NOT NULL,
  headcount INTEGER NOT NULL,
  attended INTEGER,
  status TEXT NOT NULL,
  locked INTEGER NOT NULL DEFAULT 0,
  state_reasons TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_masters (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  master_id TEXT NOT NULL REFERENCES masters(id),
  skill TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'assigned',
  assigned_at TEXT NOT NULL,
  released_at TEXT
);
CREATE TABLE IF NOT EXISTS session_materials (
  session_id TEXT NOT NULL REFERENCES sessions(id),
  material_id TEXT NOT NULL REFERENCES materials(id),
  qty_reserved INTEGER NOT NULL,
  qty_consumed INTEGER,
  status TEXT NOT NULL DEFAULT 'reserved',
  PRIMARY KEY (session_id, material_id)
);
CREATE TABLE IF NOT EXISTS rosters (
  booking_id TEXT NOT NULL REFERENCES bookings(id),
  idem_key TEXT NOT NULL,
  headcount INTEGER NOT NULL,
  age_band TEXT,
  roster_ref TEXT,
  digest TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (booking_id, idem_key)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  type TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  explanation TEXT
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
  key TEXT PRIMARY KEY,
  endpoint TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  response TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


class Store:
    """单连接 + 写锁：演示规模下保证串行化写入，避免容量竞态。"""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()
