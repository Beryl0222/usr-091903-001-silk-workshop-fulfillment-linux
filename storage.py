"""SQLite 存储层：表结构与连接管理。

只依赖标准库。所有写操作走同一把锁，保证 ThreadingHTTPServer 下串行化，
配合 UNIQUE 约束实现断网补录的幂等。
"""

import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
-- 面料批次：旗袍裁衣剩余真丝入库时登记
CREATE TABLE IF NOT EXISTS fabric_batches (
    id             TEXT PRIMARY KEY,
    code           TEXT UNIQUE,
    source         TEXT,                 -- 来源说明（哪件旗袍/订单余料）
    silk_type      TEXT,                 -- 真丝品类（素绉缎/乔其/织锦…）
    color          TEXT,
    dyeing_notes   TEXT,                 -- 染色注意事项
    cleaning_notes TEXT,                 -- 清洁注意事项
    length_cm      REAL NOT NULL,
    width_cm       REAL NOT NULL,
    handler_id     TEXT,
    handler_name   TEXT,
    received_at    TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'in_stock'
);

-- 裁片：同一批次可反复拆分，parent_id 串起完整谱系
CREATE TABLE IF NOT EXISTS fabric_pieces (
    id               TEXT PRIMARY KEY,
    code             TEXT UNIQUE,
    batch_id         TEXT NOT NULL REFERENCES fabric_batches(id),
    parent_id        TEXT REFERENCES fabric_pieces(id),
    length_cm        REAL NOT NULL,
    width_cm         REAL NOT NULL,
    status           TEXT NOT NULL,      -- available/split/used/returned/display
    event_id         TEXT,               -- 当前归属场次
    disposition_note TEXT,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pieces_batch ON fabric_pieces(batch_id);
CREATE INDEX IF NOT EXISTS idx_pieces_parent ON fabric_pieces(parent_id);
CREATE INDEX IF NOT EXISTS idx_pieces_event ON fabric_pieces(event_id);

-- 裁片流转台账：拆分、领用、退回工坊、改作展示、场次转交全部留痕
CREATE TABLE IF NOT EXISTS piece_movements (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    piece_id     TEXT NOT NULL,
    action       TEXT NOT NULL,          -- receive/split/allocate/return/display/transfer/reuse
    from_status  TEXT,
    to_status    TEXT,
    event_id     TEXT,
    handler_id   TEXT,
    handler_name TEXT,
    amount_cm2   REAL,
    note         TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_move_piece ON piece_movements(piece_id);

-- 讲师与资质
CREATE TABLE IF NOT EXISTS instructors (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    qualifications  TEXT NOT NULL DEFAULT '[]',  -- [{title, issued_by, issued_at, expires_at}]
    specialties     TEXT NOT NULL DEFAULT '[]',  -- 可带工艺
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

-- 活动方案：工艺讲解与每件作品的真丝用量标准
CREATE TABLE IF NOT EXISTS activity_plans (
    id                 TEXT PRIMARY KEY,
    title              TEXT NOT NULL,
    craft_types        TEXT NOT NULL DEFAULT '[]', -- ["手链","发簪","耳饰"]
    process_script     TEXT,                        -- 工艺讲解要点
    silk_requirements  TEXT NOT NULL DEFAULT '{}',  -- {"手链": 60, ...} 单位 cm²
    payout_rate_per_head REAL NOT NULL DEFAULT 0,   -- 讲师每人头课酬
    created_at         TEXT NOT NULL
);

-- 场次。promise_snapshot 保留改期前对顾客的原始承诺
CREATE TABLE IF NOT EXISTS events (
    id               TEXT PRIMARY KEY,
    plan_id          TEXT NOT NULL REFERENCES activity_plans(id),
    title            TEXT NOT NULL,
    start_at         TEXT NOT NULL,
    end_at           TEXT NOT NULL,
    instructor_id    TEXT REFERENCES instructors(id),
    status           TEXT NOT NULL DEFAULT 'scheduled', -- scheduled/rescheduled/cancelled/completed
    promise_snapshot TEXT,                               -- 首次排期承诺 JSON
    payout_rate_per_head REAL,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);

-- 场次修订：改期、换人、取消
CREATE TABLE IF NOT EXISTS event_revisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   TEXT NOT NULL,
    kind       TEXT NOT NULL,          -- create/reschedule/instructor_change/cancel/complete
    changes    TEXT NOT NULL DEFAULT '{}',
    created_by TEXT,
    created_at TEXT NOT NULL
);

-- 交接：材料 / 工艺讲解 / 照片授权三类责任的接手人
CREATE TABLE IF NOT EXISTS handoffs (
    id              TEXT PRIMARY KEY,
    event_id        TEXT NOT NULL,
    responsibility  TEXT NOT NULL,     -- material/process/photo
    from_party      TEXT,
    to_party        TEXT NOT NULL,
    reason          TEXT,
    created_by      TEXT,
    created_at      TEXT NOT NULL
);

-- 参与者（含未成年人与监护人信息）；choices 为当场选择制作的品类
CREATE TABLE IF NOT EXISTS participants (
    id              TEXT PRIMARY KEY,
    event_id        TEXT NOT NULL REFERENCES events(id),
    name            TEXT NOT NULL,
    is_minor        INTEGER NOT NULL DEFAULT 0,
    guardian_name   TEXT,
    guardian_contact TEXT,
    contact         TEXT,
    choices         TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'registered', -- registered/cancelled
    created_at      TEXT NOT NULL
);

-- 领料（断网补录以 client_ref 去重，绝不重复扣减）
CREATE TABLE IF NOT EXISTS allocations (
    id           TEXT PRIMARY KEY,
    client_ref   TEXT UNIQUE,
    event_id     TEXT NOT NULL,
    participant_id TEXT,
    piece_id     TEXT NOT NULL,
    area_cm2     REAL NOT NULL CHECK (area_cm2 > 0),
    purpose      TEXT,
    handler_id   TEXT,
    handler_name TEXT,
    recorded_at  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active', -- active/reversed
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alloc_event ON allocations(event_id);
CREATE INDEX IF NOT EXISTS idx_alloc_piece ON allocations(piece_id);

-- 成品（体验作品）
CREATE TABLE IF NOT EXISTS artworks (
    id             TEXT PRIMARY KEY,
    client_ref     TEXT UNIQUE,
    event_id       TEXT NOT NULL,
    participant_id TEXT,
    title          TEXT NOT NULL,
    craft_type     TEXT NOT NULL,
    created_by     TEXT,
    note           TEXT,
    recorded_at    TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_art_event ON artworks(event_id);

-- 作品用料关联：作品 → 裁片/领料
CREATE TABLE IF NOT EXISTS artwork_materials (
    artwork_id    TEXT NOT NULL REFERENCES artworks(id),
    piece_id      TEXT NOT NULL,
    allocation_id TEXT,
    area_cm2      REAL NOT NULL,
    PRIMARY KEY (artwork_id, piece_id)
);

-- 影像/故事授权：按主体 + 用途 + 期限分别取得
CREATE TABLE IF NOT EXISTS consent_grants (
    id              TEXT PRIMARY KEY,
    subject_type    TEXT NOT NULL,  -- minor_image / customer_story / artwork_photo
    subject_id      TEXT NOT NULL,  -- participant_id 或 artwork_id
    purpose         TEXT NOT NULL,  -- display/promotion/archive/...
    channel         TEXT NOT NULL DEFAULT 'any',
    granted_by      TEXT NOT NULL,
    granted_by_role TEXT NOT NULL,  -- self/guardian/staff
    valid_from      TEXT NOT NULL,
    valid_until     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'granted', -- granted/withdrawn
    withdrawn_at    TEXT,
    withdraw_reason TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_consent_subject ON consent_grants(subject_type, subject_id);

-- 展示尝试：撤回/过期后拒绝新展示并留痕
CREATE TABLE IF NOT EXISTS display_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL,
    subject_id   TEXT NOT NULL,
    purpose      TEXT NOT NULL,
    channel      TEXT,
    allowed      INTEGER NOT NULL,
    reason       TEXT,
    created_at   TEXT NOT NULL
);

-- 费用流水：费用去向（报名费、材料成本、工坊退回、讲师课酬）
CREATE TABLE IF NOT EXISTS fee_records (
    id          TEXT PRIMARY KEY,
    event_id    TEXT,
    artwork_id  TEXT,
    category    TEXT NOT NULL,  -- participant_fee/material_cost/workshop_refund/instructor_payout/other
    direction   TEXT NOT NULL,  -- in/out
    amount      REAL NOT NULL CHECK (amount >= 0),
    note        TEXT,
    created_by  TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fee_event ON fee_records(event_id);
CREATE INDEX IF NOT EXISTS idx_fee_artwork ON fee_records(artwork_id);

-- 边角料再利用登记，供设计师核量
CREATE TABLE IF NOT EXISTS reuse_records (
    id          TEXT PRIMARY KEY,
    piece_id    TEXT NOT NULL,
    product     TEXT NOT NULL,
    area_cm2    REAL NOT NULL CHECK (area_cm2 > 0),
    recorded_by TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL
);

-- 泛化幂等表：同一 client_ref 的补录请求只生效一次，并回放首次结果
CREATE TABLE IF NOT EXISTS idempotent_requests (
    client_ref  TEXT PRIMARY KEY,
    op          TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """持有单连接 + 锁；SQLite 单文件，便于现场离线运行。"""

    def __init__(self, path=":memory:"):
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def lock(self):
        return self._lock

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def commit(self):
        self.conn.commit()

    def close(self):
        with self._lock:
            self.conn.close()
