"""
Tiny storage layer.

  * No DATABASE_URL  -> SQLite file (data.db) next to the bot. Fine on a PC/VPS.
  * DATABASE_URL set -> Postgres (e.g. a free Neon/Supabase database). Use this on Render,
    because Render's free disk is wiped on every restart.

Only two things are stored: a key/value table (bot state, settings, lease) and a short
log of what was sent. Connections are opened per call; the bot only touches the DB a few
times per check, and this avoids stale connections to databases that auto-suspend.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

LOG_KEEP = 500


class Store:
    def __init__(self, url: str = "", sqlite_path: Path | None = None):
        self.url = url
        self.pg = url.startswith(("postgres://", "postgresql://"))
        self.path = str(sqlite_path or Path(__file__).resolve().parent / "data.db")
        self.ph = "%s" if self.pg else "?"
        if self.pg:
            import psycopg2  # imported lazily so local use doesn't need it
            self._psycopg2 = psycopg2
        self._init()

    # ---- connection helpers
    def _connect(self):
        if self.pg:
            return self._psycopg2.connect(self.url, connect_timeout=15)
        conn = sqlite3.connect(self.path, timeout=15)
        return conn

    def _run(self, sql: str, params: tuple = (), fetch: str = "") :
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall() if fetch == "all" else cur.fetchone() if fetch == "one" else None
            conn.commit()
            return rows
        finally:
            conn.close()

    def _init(self) -> None:
        pk = "SERIAL PRIMARY KEY" if self.pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        self._run("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self._run(
            f"CREATE TABLE IF NOT EXISTS log (id {pk}, ts BIGINT NOT NULL, post_id TEXT, "
            "platform TEXT, status TEXT, detail TEXT)"
        )

    # ---- key/value (JSON)
    def get_json(self, key: str, default=None):
        row = self._run(f"SELECT value FROM kv WHERE key = {self.ph}", (key,), "one")
        if not row:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            raise ValueError(f"stored value for '{key}' is corrupt")

    def set_json(self, key: str, value) -> None:
        self._run(
            f"INSERT INTO kv (key, value) VALUES ({self.ph}, {self.ph}) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # ---- send log
    def add_log(self, post_id: str, platform: str, status: str, detail: str = "") -> None:
        self._run(
            f"INSERT INTO log (ts, post_id, platform, status, detail) VALUES "
            f"({self.ph}, {self.ph}, {self.ph}, {self.ph}, {self.ph})",
            (int(time.time()), post_id, platform, status, detail[:300]),
        )
        self._run(
            f"DELETE FROM log WHERE id <= (SELECT COALESCE(MAX(id), 0) FROM log) - {LOG_KEEP}"
        )

    def recent_logs(self, n: int = 30) -> list[dict]:
        rows = self._run(
            f"SELECT ts, post_id, platform, status, detail FROM log ORDER BY id DESC LIMIT {int(n)}",
            fetch="all",
        ) or []
        return [dict(ts=r[0], post_id=r[1], platform=r[2], status=r[3], detail=r[4]) for r in rows]


def open_store() -> Store:
    return Store(os.environ.get("DATABASE_URL", "").strip())
