"""SQLite storage. Every user-owned table carries user_id and every query filters on it."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    google_sub TEXT NOT NULL UNIQUE,
    email TEXT,
    name TEXT,
    picture TEXT,
    is_dev INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_login_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS photos (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('meal', 'body')),
    path TEXT NOT NULL,
    thumb_path TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    recognition TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, client_id)
);

CREATE TABLE IF NOT EXISTS meals (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id TEXT NOT NULL,
    date TEXT NOT NULL,
    time TEXT,
    meal_type TEXT NOT NULL,
    items TEXT NOT NULL,
    memo TEXT NOT NULL DEFAULT '',
    photo_id TEXT REFERENCES photos(id) ON DELETE SET NULL,
    nutrition TEXT,
    nutrition_status TEXT NOT NULL DEFAULT 'none',
    analysis TEXT,
    analysis_error TEXT,
    analysis_started_at TEXT,
    analyzed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (user_id, client_id)
);
CREATE INDEX IF NOT EXISTS meals_user_date ON meals(user_id, date);

CREATE TABLE IF NOT EXISTS body_logs (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id TEXT NOT NULL,
    date TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    photo_id TEXT REFERENCES photos(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (user_id, client_id)
);
CREATE INDEX IF NOT EXISTS body_user_date ON body_logs(user_id, date);

CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('warmup', 'movement')),
    created_at TEXT NOT NULL,
    UNIQUE (user_id, date, kind)
);

CREATE TABLE IF NOT EXISTS day_meta (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    complete INTEGER NOT NULL DEFAULT 0,
    coach TEXT,
    coach_input_hash TEXT,
    coach_created_at TEXT,
    PRIMARY KEY (user_id, date)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(value):
    return json.loads(value) if value else None


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Writes are serialized so idempotency checks + inserts never race.
        self.write_lock = threading.RLock()
        self._raw().executescript(SCHEMA)

    def connect(self) -> "_ConnContext":
        return _ConnContext(self._raw())

    def _raw(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 15000")
            self._local.conn = conn
        return conn

    def close_thread_connection(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


class _ConnContext:
    """Small wrapper so `with db.connect() as conn` runs a transaction."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False
