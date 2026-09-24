"""SQLite local: snapshots, watchlist y caché. No escribe en Wallapop."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    keyword TEXT,
    title TEXT,
    price_amount REAL,
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
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    def __init__(self, path: Path, ttl_days: int = 90) -> None:
        self.path = path
        self.ttl_days = ttl_days
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self.purge()

    def close(self) -> None:
        self._conn.close()

    def purge(self) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.ttl_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur = self._conn.execute("DELETE FROM snapshots WHERE captured_at < ?", (cutoff,))
        self._conn.commit()
        return cur.rowcount

    def add_snapshot(self, row: dict[str, Any]) -> None:
        captured = row.get("captured_at") or utcnow()
        # Un snapshot por anuncio y minuto para no duplicar ráfagas.
        minute = captured[:16]
        existing = self._conn.execute(
            "SELECT id FROM snapshots WHERE item_id = ? AND substr(captured_at, 1, 16) = ?",
            (row["item_id"], minute),
        ).fetchone()
        if existing:
            return
        self._conn.execute(
            """
            INSERT INTO snapshots (
                item_id, captured_at, keyword, title, price_amount, currency,
                available, reserved, views, favorites, conversations,
                metrics_method, reliability, seller_id, category_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        self._conn.commit()

    def history(self, *, item_id: str | None = None, keyword: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if item_id:
            rows = self._conn.execute(
                "SELECT * FROM snapshots WHERE item_id = ? ORDER BY captured_at DESC LIMIT ?",
                (item_id, limit),
            ).fetchall()
        elif keyword:
            rows = self._conn.execute(
                "SELECT * FROM snapshots WHERE keyword = ? ORDER BY captured_at DESC LIMIT ?",
                (keyword.lower(), limit),
            ).fetchall()
        else:
            return []
        return [dict(row) for row in rows]

    def previous_ids(self, keyword: str, before_iso: str) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT * FROM snapshots
            WHERE keyword = ? AND captured_at < ?
            ORDER BY captured_at DESC
            LIMIT 400
            """,
            (keyword.lower(), before_iso),
        ).fetchall()
        found: dict[str, dict[str, Any]] = {}
        for row in rows:
            found.setdefault(row["item_id"], dict(row))
        return found

    def watch_add(self, kind: str, key: str, label: str | None) -> None:
        self._conn.execute(
            """
            INSERT INTO watchlist (kind, key, label, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(kind, key) DO UPDATE SET label = excluded.label
            """,
            (kind, key, label, utcnow()),
        )
        self._conn.commit()

    def watch_remove(self, kind: str, key: str) -> int:
        cur = self._conn.execute("DELETE FROM watchlist WHERE kind = ? AND key = ?", (kind, key))
        self._conn.commit()
        return cur.rowcount

    def watch_list(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM watchlist ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def cache_categories(self, payload: dict[str, Any]) -> None:
        blob = json.dumps(payload, ensure_ascii=False)
        self._conn.execute(
            """
            INSERT INTO category_cache (id, fetched_at, payload) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET fetched_at = excluded.fetched_at, payload = excluded.payload
            """,
            (utcnow(), blob),
        )
        self._conn.commit()

    def cached_categories(self, max_age_hours: int = 24) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT fetched_at, payload FROM category_cache WHERE id = 1").fetchone()
        if not row:
            return None
        fetched = datetime.strptime(row["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - fetched > timedelta(hours=max_age_hours):
            return None
        return json.loads(row["payload"])

    def cache_metrics(self, item_id: str, payload: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO metric_cache (item_id, fetched_at, payload) VALUES (?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET fetched_at = excluded.fetched_at, payload = excluded.payload
            """,
            (item_id, utcnow(), json.dumps(payload, ensure_ascii=False)),
        )
        self._conn.commit()

    def cached_metrics(self, item_id: str, max_age_minutes: int = 30) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT fetched_at, payload FROM metric_cache WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if not row:
            return None
        fetched = datetime.strptime(row["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - fetched > timedelta(minutes=max_age_minutes):
            return None
        return json.loads(row["payload"])
