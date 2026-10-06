"""SQLite 持久化边界。

只保存两类数据：

1. ``event`` —— 不可变业务事件流（含幂等收件箱 ``inbox``），是唯一事实来源；
2. ``proj_*`` —— 由事件流重放得到的投影表，任何时候都可以整表重建。

因此「崩溃恢复」等价于：重新打开数据库后用事件流重放一遍投影。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .domain import Event, Record


class DuplicateRequest(Exception):
    """request_id 已处理过，携带首次处理结果。"""

    def __init__(self, result: dict) -> None:
        super().__init__(f"重复请求: {result.get('biz_no')}")
        self.result = result


class NaturalDuplicate(Exception):
    """同一自然业务事实已落库（终端换了 request_id 重发/补传）。"""

    def __init__(self, event: Event) -> None:
        super().__init__(f"重复业务事实: {event.biz_no}")
        self.event = event


SCHEMA = """
CREATE TABLE IF NOT EXISTS benefit_event (
    record_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    biz_no         TEXT NOT NULL UNIQUE,
    request_id     TEXT UNIQUE,
    type           TEXT NOT NULL,
    entitlement_id TEXT NOT NULL DEFAULT '',
    activity_id    TEXT NOT NULL DEFAULT '',
    version        INTEGER NOT NULL DEFAULT 0,
    community_id   TEXT NOT NULL DEFAULT '',
    store_id       TEXT NOT NULL DEFAULT '',
    amount         INTEGER NOT NULL DEFAULT 0,
    occurred_at    TEXT NOT NULL,
    recorded_at    TEXT NOT NULL,
    natural_key    TEXT NOT NULL UNIQUE,
    payload        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_event_ent ON event(entitlement_id);
CREATE INDEX IF NOT EXISTS idx_event_store_date ON event(store_id, occurred_at);

CREATE TABLE IF NOT EXISTS inbox (
    request_id TEXT PRIMARY KEY,
    biz_no     TEXT NOT NULL,
    result     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proj_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proj_activity (
    activity_id  TEXT NOT NULL,
    version      INTEGER NOT NULL,
    community_id TEXT NOT NULL,
    face_value   INTEGER NOT NULL,
    rate         TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    PRIMARY KEY (activity_id, version)
);
CREATE TABLE IF NOT EXISTS proj_entitlement (
    entitlement_id  TEXT PRIMARY KEY,
    activity_id     TEXT NOT NULL,
    version         INTEGER NOT NULL,
    face_value      INTEGER NOT NULL,
    status          TEXT NOT NULL,
    winner_store    TEXT NOT NULL DEFAULT '',
    redeem_seq      INTEGER NOT NULL DEFAULT 0,
    settle_amount   INTEGER NOT NULL DEFAULT 0,
    refunded_face   INTEGER NOT NULL DEFAULT 0,
    refunded_amount INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS proj_ledger (
    entry_id       INTEGER PRIMARY KEY,
    event_seq      INTEGER NOT NULL,
    biz_no         TEXT NOT NULL,
    entitlement_id TEXT NOT NULL,
    store_id       TEXT NOT NULL,
    community_id   TEXT NOT NULL,
    activity_id    TEXT NOT NULL,
    version        INTEGER NOT NULL,
    biz_date       TEXT NOT NULL,
    kind           TEXT NOT NULL,
    amount         INTEGER NOT NULL,
    status         TEXT NOT NULL,
    bound          INTEGER NOT NULL DEFAULT 0,
    reverses       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ledger_event ON proj_ledger(event_seq);
CREATE INDEX IF NOT EXISTS idx_ledger_store ON proj_ledger(store_id, biz_date);
CREATE INDEX IF NOT EXISTS idx_ledger_ent ON proj_ledger(entitlement_id);

CREATE TABLE IF NOT EXISTS proj_settlement (
    biz_no          TEXT PRIMARY KEY,
    event_seq       INTEGER NOT NULL,
    store_id        TEXT NOT NULL,
    through_date    TEXT NOT NULL,
    amount          INTEGER NOT NULL,
    frozen_amount   INTEGER NOT NULL DEFAULT 0,
    entry_seqs      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS proj_freeze (
    biz_no       TEXT PRIMARY KEY,
    event_seq    INTEGER NOT NULL,
    scope        TEXT NOT NULL,
    entitlement_id TEXT NOT NULL DEFAULT '',
    store_id     TEXT NOT NULL DEFAULT '',
    biz_date     TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1,
    unfreeze_biz_no TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS proj_dispute (
    entitlement_id TEXT PRIMARY KEY,
    winner_store   TEXT NOT NULL,
    loser_store    TEXT NOT NULL,
    winner_biz_no  TEXT NOT NULL,
    loser_biz_no   TEXT NOT NULL,
    loser_seq      INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS proj_rejected (
    event_seq INTEGER PRIMARY KEY,
    biz_no    TEXT NOT NULL,
    reason    TEXT NOT NULL
);
"""

PROJ_TABLES = [
    "proj_rejected", "proj_dispute", "proj_freeze", "proj_settlement",
    "proj_ledger", "proj_entitlement", "proj_activity", "proj_meta",
]


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    # -- 事务 ---------------------------------------------------------------

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """串行化写事务：命令处理 + 投影重建在同一事务内原子提交。"""
        conn = self.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # -- 事件流 -------------------------------------------------------------

    def next_seq(self, conn: sqlite3.Connection) -> int:
        return conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM event").fetchone()[0]

    def append_event(self, conn: sqlite3.Connection, event: Event) -> None:
        row = event.to_row()
        try:
            conn.execute(
                """INSERT INTO event(seq, biz_no, request_id, type, entitlement_id,
                       activity_id, version, community_id, store_id, amount,
                       occurred_at, recorded_at, natural_key, payload)
                   VALUES(:seq,:biz_no,:request_id,:type,:entitlement_id,
                       :activity_id,:version,:community_id,:store_id,:amount,
                       :occurred_at,:recorded_at,:natural_key,:payload)""",
                row,
            )
        except sqlite3.IntegrityError as exc:
            msg = str(exc)
            if event.request_id:
                hit = conn.execute(
                    "SELECT result FROM inbox WHERE request_id=?",
                    (event.request_id,),
                ).fetchone()
                if hit:
                    raise DuplicateRequest(json.loads(hit["result"])) from exc
                hit = conn.execute(
                    "SELECT * FROM event WHERE request_id=?", (event.request_id,),
                ).fetchone()
                if hit:
                    raise DuplicateRequest(
                        {"biz_no": hit["biz_no"], "type": hit["type"],
                         "duplicated": True}
                    ) from exc
            hit = conn.execute(
                "SELECT * FROM event WHERE natural_key=?", (row["natural_key"],),
            ).fetchone()
            if hit:
                raise NaturalDuplicate(Event.from_row(hit)) from exc
            raise

    def remember(self, conn: sqlite3.Connection, request_id: str,
                 biz_no: str, result: dict) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO inbox(request_id, biz_no, result) VALUES(?,?,?)",
            (request_id, biz_no, json.dumps(result, ensure_ascii=False, sort_keys=True)),
        )

    def inbox_lookup(self, request_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT result FROM inbox WHERE request_id=?", (request_id,)
        ).fetchone()
        return json.loads(row["result"]) if row else None

    def list_events(self) -> list[Event]:
        rows = self.connection.execute("SELECT * FROM event ORDER BY seq").fetchall()
        return [Event.from_row(r) for r in rows]

    # -- 投影重建 -----------------------------------------------------------

    def reset_projection(self, conn: sqlite3.Connection) -> None:
        for table in PROJ_TABLES:
            conn.execute(f"DELETE FROM {table}")

    def load_projection(self, projection) -> None:
        """把投影结果整体写回（须在 tx 内调用）。"""
        conn = self.connection
        conn.executemany(
            """INSERT INTO proj_activity(activity_id, version, community_id,
                   face_value, rate, effective_from)
               VALUES(:activity_id,:version,:community_id,:face_value,:rate,:effective_from)""",
            list(projection.activities.values()),
        )
        conn.executemany(
            """INSERT INTO proj_entitlement(entitlement_id, activity_id, version,
                   face_value, status, winner_store, redeem_seq, settle_amount,
                   refunded_face, refunded_amount)
               VALUES(:entitlement_id,:activity_id,:version,:face_value,:status,
                   :winner_store,:redeem_seq,:settle_amount,:refunded_face,:refunded_amount)""",
            list(projection.entitlements.values()),
        )
        conn.executemany(
            """INSERT INTO proj_ledger(entry_id, event_seq, biz_no, entitlement_id,
                   store_id, community_id, activity_id, version, biz_date, kind,
                   amount, status, bound, reverses)
               VALUES(:entry_id,:event_seq,:biz_no,:entitlement_id,:store_id,
                   :community_id,:activity_id,:version,:biz_date,:kind,:amount,
                   :status,:bound,:reverses)""",
            projection.ledger_rows(),
        )
        conn.executemany(
            """INSERT INTO proj_settlement(biz_no, event_seq, store_id, through_date,
                   amount, frozen_amount, entry_seqs, status)
               VALUES(:biz_no,:event_seq,:store_id,:through_date,:amount,
                   :frozen_amount,:entry_seqs,:status)""",
            projection.settlement_rows(),
        )
        conn.executemany(
            """INSERT INTO proj_freeze(biz_no, event_seq, scope, entitlement_id,
                   store_id, biz_date, reason, active, unfreeze_biz_no)
               VALUES(:biz_no,:event_seq,:scope,:entitlement_id,:store_id,:biz_date,
                   :reason,:active,:unfreeze_biz_no)""",
            projection.freeze_rows(),
        )
        conn.executemany(
            """INSERT INTO proj_dispute(entitlement_id, winner_store, loser_store,
                   winner_biz_no, loser_biz_no, loser_seq, status)
               VALUES(:entitlement_id,:winner_store,:loser_store,:winner_biz_no,
                   :loser_biz_no,:loser_seq,:status)""",
            projection.dispute_rows(),
        )
        conn.executemany(
            "INSERT INTO proj_rejected(event_seq, biz_no, reason) VALUES(:event_seq,:biz_no,:reason)",
            projection.rejected_rows(),
        )
        conn.execute(
            "INSERT OR REPLACE INTO proj_meta(key,value) VALUES('seq',?)",
            (str(projection.seq),),
        )

    # -- 旧版基线能力 --------------------------------------------------------

    def save_record(self, conn: sqlite3.Connection, record: Record) -> Record:
        value = record.with_timestamp()
        conn.execute(
            "INSERT INTO benefit_event(record_id, owner_id, state, created_at) VALUES(?,?,?,?)",
            (value.record_id, value.owner_id, value.state, value.created_at),
        )
        return value

    def get_record(self, record_id: str) -> Record | None:
        row = self.connection.execute(
            "SELECT record_id, owner_id, state, created_at FROM benefit_event WHERE record_id=?",
            (record_id,),
        ).fetchone()
        return Record(**dict(row)) if row else None

    def close(self) -> None:
        self.connection.close()
