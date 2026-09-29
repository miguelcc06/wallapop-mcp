"""PostgreSQL local: snapshots, watchlist y caché. No escribe en Wallapop."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
import psycopg
from psycopg.rows import dict_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id BIGSERIAL PRIMARY KEY,
    item_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    keyword TEXT,
    title TEXT,
    price_amount DOUBLE PRECISION,
    currency TEXT,
    available INTEGER,
    reserved INTEGER,
    views INTEGER,
    favorites INTEGER,
    conversations INTEGER,
    metrics_method TEXT,
    reliability TEXT,
    seller_id TEXT,
    category_id TEXT,
    UNIQUE(item_id, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_snap_item ON snapshots(item_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_snap_kw ON snapshots(keyword, captured_at);

CREATE TABLE IF NOT EXISTS watchlist (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(kind, key)
);

CREATE TABLE IF NOT EXISTS category_cache (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    fetched_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metric_cache (
    item_id TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, dsn: str, ttl_days: int = 90) -> None:
        self.dsn = dsn
        self.ttl_days = ttl_days
        self._conn = psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True)
        with self._conn.cursor() as cur:
            cur.execute(SCHEMA)
        self.purge()

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()

    def purge(self) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.ttl_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM snapshots WHERE captured_at < %s", (cutoff,))
            return cur.rowcount

    def add_snapshot(self, row: dict[str, Any]) -> None:
        captured = row.get("captured_at") or utcnow()
        minute = captured[:16]
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM snapshots WHERE item_id = %s AND substring(captured_at from 1 for 16) = %s",
                (row["item_id"], minute),
            )
            if cur.fetchone():
                return
            cur.execute(
                """
                INSERT INTO snapshots (
                    item_id, captured_at, keyword, title, price_amount, currency,
                    available, reserved, views, favorites, conversations,
                    metrics_method, reliability, seller_id, category_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (item_id, captured_at) DO NOTHING
                """,
                (
                    row["item_id"],
                    captured,
                    row.get("keyword"),
                    row.get("title"),
                    row.get("price_amount"),
                    row.get("currency"),
                    row.get("available", 1),
                    row.get("reserved"),
                    row.get("views"),
                    row.get("favorites"),
                    row.get("conversations"),
                    row.get("metrics_method"),
                    row.get("reliability"),
                    row.get("seller_id"),
                    row.get("category_id"),
                ),
            )

    def history(self, *, item_id: str | None = None, keyword: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            if item_id:
                cur.execute(
                    "SELECT * FROM snapshots WHERE item_id = %s ORDER BY captured_at DESC LIMIT %s",
                    (item_id, limit),
                )
            elif keyword:
                cur.execute(
                    "SELECT * FROM snapshots WHERE lower(keyword) = %s ORDER BY captured_at DESC LIMIT %s",
                    (keyword.lower(), limit),
                )
            else:
                return []
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def previous_ids(self, keyword: str, before_iso: str) -> dict[str, dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM snapshots
                WHERE lower(keyword) = %s AND captured_at < %s
                ORDER BY captured_at DESC
                LIMIT 400
                """,
                (keyword.lower(), before_iso),
            )
            rows = cur.fetchall()
            found: dict[str, dict[str, Any]] = {}
            for row in rows:
                d = dict(row)
                found.setdefault(d["item_id"], d)
            return found

    def watch_add(self, kind: str, key: str, label: str | None) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO watchlist (kind, key, label, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(kind, key) DO UPDATE SET label = EXCLUDED.label
                """,
                (kind, key, label, utcnow()),
            )

    def watch_remove(self, kind: str, key: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM watchlist WHERE kind = %s AND key = %s", (kind, key))
            return cur.rowcount

    def watch_list(self) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM watchlist ORDER BY created_at DESC")
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def cache_categories(self, payload: dict[str, Any]) -> None:
        blob = json.dumps(payload, ensure_ascii=False)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO category_cache (id, fetched_at, payload) VALUES (1, %s, %s)
                ON CONFLICT(id) DO UPDATE SET fetched_at = EXCLUDED.fetched_at, payload = EXCLUDED.payload
                """,
                (utcnow(), blob),
            )

    def cached_categories(self, max_age_hours: int = 24) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT fetched_at, payload FROM category_cache WHERE id = 1")
            row = cur.fetchone()
            if not row:
                return None
            fetched = datetime.strptime(row["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - fetched > timedelta(hours=max_age_hours):
                return None
            return json.loads(row["payload"])

    def cache_metrics(self, item_id: str, payload: dict[str, Any]) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO metric_cache (item_id, fetched_at, payload) VALUES (%s, %s, %s)
                ON CONFLICT(item_id) DO UPDATE SET fetched_at = EXCLUDED.fetched_at, payload = EXCLUDED.payload
                """,
                (item_id, utcnow(), json.dumps(payload, ensure_ascii=False)),
            )

    def cached_metrics(self, item_id: str, max_age_minutes: int = 30) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT fetched_at, payload FROM metric_cache WHERE item_id = %s",
                (item_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            fetched = datetime.strptime(row["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - fetched > timedelta(minutes=max_age_minutes):
                return None
            return json.loads(row["payload"])
