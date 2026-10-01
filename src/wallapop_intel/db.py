"""PostgreSQL local: snapshots, watchlist y caché. No escribe en Wallapop."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        id BIGSERIAL PRIMARY KEY,
        item_id TEXT NOT NULL,
        captured_at TIMESTAMPTZ NOT NULL,
        keyword TEXT,
        title TEXT,
        price_amount DOUBLE PRECISION,
        currency TEXT,
        available BOOLEAN,
        reserved BOOLEAN,
        views INTEGER,
        favorites INTEGER,
        conversations INTEGER,
        metrics_method TEXT,
        reliability TEXT,
        seller_id TEXT,
        category_id TEXT,
        UNIQUE (item_id, captured_at)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_snap_item ON snapshots (item_id, captured_at)",
    "CREATE INDEX IF NOT EXISTS idx_snap_kw ON snapshots (keyword, captured_at)",
    """
    CREATE TABLE IF NOT EXISTS watchlist (
        id BIGSERIAL PRIMARY KEY,
        kind TEXT NOT NULL,
        key TEXT NOT NULL,
        label TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        UNIQUE (kind, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS category_cache (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        fetched_at TIMESTAMPTZ NOT NULL,
        payload TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS metric_cache (
        item_id TEXT PRIMARY KEY,
        fetched_at TIMESTAMPTZ NOT NULL,
        payload TEXT NOT NULL
    )
    """,
)

_TIMESTAMPTZ_COLUMNS = (
    ("snapshots", "captured_at"),
    ("watchlist", "created_at"),
    ("category_cache", "fetched_at"),
    ("metric_cache", "fetched_at"),
)
_BOOLEAN_COLUMNS = (
    ("snapshots", "available"),
    ("snapshots", "reserved"),
)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_ts(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_bool(value: Any, default: bool | None = None) -> bool | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(int(value))
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "on"}
    return default


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("captured_at", "created_at", "fetched_at"):
        value = out.get(key)
        if isinstance(value, datetime):
            out[key] = format_ts(value)
    for key in ("available", "reserved"):
        value = out.get(key)
        if isinstance(value, bool):
            out[key] = int(value)
    return out


class Store:
    def __init__(
        self,
        dsn: str,
        ttl_days: int = 90,
        *,
        schema: str | None = None,
        min_size: int = 1,
        max_size: int = 4,
    ) -> None:
        if schema is not None and not _SCHEMA_RE.fullmatch(schema):
            raise ValueError(f"Nombre de esquema no válido: {schema}")
        self.dsn = dsn
        self.ttl_days = ttl_days
        self.schema = schema
        self._pool: ConnectionPool | None = None
        created_schema = False
        try:
            if schema:
                self._create_schema()
                created_schema = True
            kwargs: dict[str, Any] = {"row_factory": dict_row, "connect_timeout": 5}
            if schema:
                kwargs["options"] = f"-c search_path={schema}"
            self._pool = ConnectionPool(
                dsn,
                min_size=min_size,
                max_size=max(max_size, min_size),
                kwargs=kwargs,
                open=True,
                timeout=10,
                name="wallapop-intel",
            )
            self._pool.wait(timeout=10)
            self._ensure_schema()
            self.purge()
        except Exception as exc:
            self.close()
            if created_schema:
                self._drop_schema()
            raise RuntimeError(
                "No se pudo abrir el pool de PostgreSQL. "
                "Revisa WALLAPOP_DATABASE_URL (o DATABASE_URL) y que exista la base wallapop_intel. "
                f"Detalle: {exc}"
            ) from exc

    def close(self) -> None:
        pool = self._pool
        self._pool = None
        if pool is not None:
            pool.close()

    def drop_schema(self) -> None:
        """Cierra el pool y borra el esquema de test, si se creó uno."""
        self.close()
        if self.schema:
            self._drop_schema()

    def _create_schema(self) -> None:
        assert self.schema is not None
        with psycopg.connect(self.dsn, autocommit=True, connect_timeout=5) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))

    def _drop_schema(self) -> None:
        assert self.schema is not None
        with psycopg.connect(self.dsn, autocommit=True, connect_timeout=5) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(self.schema)))

    def _with_cursor(self, fn: Callable[[psycopg.Cursor[dict[str, Any]]], Any]) -> Any:
        if self._pool is None:
            raise RuntimeError("El pool de PostgreSQL está cerrado.")
        with self._pool.connection() as conn:
            try:
                with conn.cursor() as cur:
                    result = fn(cur)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def _ensure_schema(self) -> None:
        def apply(cur: psycopg.Cursor[dict[str, Any]]) -> None:
            for statement in SCHEMA_STATEMENTS:
                cur.execute(statement)
            self._upgrade_types(cur)

        self._with_cursor(apply)

    def _upgrade_types(self, cur: psycopg.Cursor[dict[str, Any]]) -> None:
        for table, column in _TIMESTAMPTZ_COLUMNS:
            kind = self._column_type(cur, table, column)
            if kind in {"text", "character varying"}:
                cur.execute(
                    sql.SQL("ALTER TABLE {} ALTER COLUMN {} TYPE timestamptz USING {}::timestamptz").format(
                        sql.Identifier(table),
                        sql.Identifier(column),
                        sql.Identifier(column),
                    )
                )
        for table, column in _BOOLEAN_COLUMNS:
            kind = self._column_type(cur, table, column)
            if kind == "integer":
                cur.execute(
                    sql.SQL(
                        "ALTER TABLE {} ALTER COLUMN {} TYPE boolean "
                        "USING CASE WHEN {} IS NULL THEN NULL ELSE {} <> 0 END"
                    ).format(
                        sql.Identifier(table),
                        sql.Identifier(column),
                        sql.Identifier(column),
                        sql.Identifier(column),
                    )
                )

    def _column_type(self, cur: psycopg.Cursor[dict[str, Any]], table: str, column: str) -> str | None:
        cur.execute(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = %s
              AND column_name = %s
            """,
            (table, column),
        )
        row = cur.fetchone()
        if not row:
            return None
        return str(row["data_type"])

    def purge(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.ttl_days)

        def run(cur: psycopg.Cursor[dict[str, Any]]) -> int:
            cur.execute("DELETE FROM snapshots WHERE captured_at < %s", (cutoff,))
            return cur.rowcount

        return self._with_cursor(run)

    def add_snapshot(self, row: dict[str, Any]) -> None:
        captured = parse_ts(row.get("captured_at") or utcnow())

        def run(cur: psycopg.Cursor[dict[str, Any]]) -> None:
            cur.execute(
                """
                SELECT id FROM snapshots
                WHERE item_id = %s
                  AND date_trunc('minute', captured_at AT TIME ZONE 'UTC')
                      = date_trunc('minute', %s AT TIME ZONE 'UTC')
                """,
                (row["item_id"], captured),
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
                    _as_bool(row.get("available"), default=True),
                    _as_bool(row.get("reserved")),
                    row.get("views"),
                    row.get("favorites"),
                    row.get("conversations"),
                    row.get("metrics_method"),
                    row.get("reliability"),
                    row.get("seller_id"),
                    row.get("category_id"),
                ),
            )

        self._with_cursor(run)

    def history(
        self,
        *,
        item_id: str | None = None,
        keyword: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        def run(cur: psycopg.Cursor[dict[str, Any]]) -> list[dict[str, Any]]:
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
            return [_public_row(row) for row in cur.fetchall()]

        return self._with_cursor(run)

    def previous_ids(self, keyword: str, before_iso: str) -> dict[str, dict[str, Any]]:
        before = parse_ts(before_iso)

        def run(cur: psycopg.Cursor[dict[str, Any]]) -> dict[str, dict[str, Any]]:
            cur.execute(
                """
                SELECT * FROM snapshots
                WHERE lower(keyword) = %s AND captured_at < %s
                ORDER BY captured_at DESC
                LIMIT 400
                """,
                (keyword.lower(), before),
            )
            found: dict[str, dict[str, Any]] = {}
            for row in cur.fetchall():
                public = _public_row(row)
                found.setdefault(public["item_id"], public)
            return found

        return self._with_cursor(run)

    def watch_add(self, kind: str, key: str, label: str | None) -> None:
        def run(cur: psycopg.Cursor[dict[str, Any]]) -> None:
            cur.execute(
                """
                INSERT INTO watchlist (kind, key, label, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (kind, key) DO UPDATE SET label = EXCLUDED.label
                """,
                (kind, key, label, datetime.now(timezone.utc)),
            )

        self._with_cursor(run)

    def watch_remove(self, kind: str, key: str) -> int:
        def run(cur: psycopg.Cursor[dict[str, Any]]) -> int:
            cur.execute("DELETE FROM watchlist WHERE kind = %s AND key = %s", (kind, key))
            return cur.rowcount

        return self._with_cursor(run)

    def watch_list(self) -> list[dict[str, Any]]:
        def run(cur: psycopg.Cursor[dict[str, Any]]) -> list[dict[str, Any]]:
            cur.execute("SELECT * FROM watchlist ORDER BY created_at DESC")
            return [_public_row(row) for row in cur.fetchall()]

        return self._with_cursor(run)

    def cache_categories(self, payload: dict[str, Any]) -> None:
        blob = json.dumps(payload, ensure_ascii=False)

        def run(cur: psycopg.Cursor[dict[str, Any]]) -> None:
            cur.execute(
                """
                INSERT INTO category_cache (id, fetched_at, payload) VALUES (1, %s, %s)
                ON CONFLICT (id) DO UPDATE SET fetched_at = EXCLUDED.fetched_at, payload = EXCLUDED.payload
                """,
                (datetime.now(timezone.utc), blob),
            )

        self._with_cursor(run)

    def cached_categories(self, max_age_hours: int = 24) -> dict[str, Any] | None:
        def run(cur: psycopg.Cursor[dict[str, Any]]) -> dict[str, Any] | None:
            cur.execute("SELECT fetched_at, payload FROM category_cache WHERE id = 1")
            row = cur.fetchone()
            if not row:
                return None
            fetched = parse_ts(row["fetched_at"])
            if datetime.now(timezone.utc) - fetched > timedelta(hours=max_age_hours):
                return None
            return json.loads(row["payload"])

        return self._with_cursor(run)

    def cache_metrics(self, item_id: str, payload: dict[str, Any]) -> None:
        blob = json.dumps(payload, ensure_ascii=False)

        def run(cur: psycopg.Cursor[dict[str, Any]]) -> None:
            cur.execute(
                """
                INSERT INTO metric_cache (item_id, fetched_at, payload) VALUES (%s, %s, %s)
                ON CONFLICT (item_id) DO UPDATE SET fetched_at = EXCLUDED.fetched_at, payload = EXCLUDED.payload
                """,
                (item_id, datetime.now(timezone.utc), blob),
            )

        self._with_cursor(run)

    def cached_metrics(self, item_id: str, max_age_minutes: int = 30) -> dict[str, Any] | None:
        def run(cur: psycopg.Cursor[dict[str, Any]]) -> dict[str, Any] | None:
            cur.execute(
                "SELECT fetched_at, payload FROM metric_cache WHERE item_id = %s",
                (item_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            fetched = parse_ts(row["fetched_at"])
            if datetime.now(timezone.utc) - fetched > timedelta(minutes=max_age_minutes):
                return None
            return json.loads(row["payload"])

        return self._with_cursor(run)
