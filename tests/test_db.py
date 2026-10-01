"""Persistencia PostgreSQL. Se omite si la base no está accesible (CI sin Postgres)."""

from __future__ import annotations

import uuid

import psycopg
import pytest

from wallapop_intel.config import resolve_database_url
from wallapop_intel.db import Store


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


@pytest.fixture
def store():
    dsn = resolve_database_url()
    if not _reachable(dsn):
        pytest.skip("PostgreSQL no disponible; el test de persistencia se omite")
    schema = f"wp_test_{uuid.uuid4().hex[:12]}"
    db = Store(dsn, ttl_days=90, schema=schema, min_size=1, max_size=2)
    try:
        yield db
    finally:
        db.drop_schema()


def test_snapshot_dedup_watch_and_cache(store: Store) -> None:
    store.add_snapshot(
        {"item_id": "abc", "captured_at": "2026-09-24T18:00:00Z", "price_amount": 10, "keyword": "ps5"}
    )
    store.add_snapshot(
        {"item_id": "abc", "captured_at": "2026-09-24T18:00:30Z", "price_amount": 9, "keyword": "ps5"}
    )
    rows = store.history(item_id="abc")
    assert len(rows) == 1
    assert rows[0]["captured_at"] == "2026-09-24T18:00:00Z"
    assert rows[0]["price_amount"] == 10

    store.watch_add("keyword", "ps5", "consola")
    store.watch_add("keyword", "ps5", "otra")
    listed = store.watch_list()
    assert len(listed) == 1
    assert listed[0]["label"] == "otra"
    assert listed[0]["created_at"].endswith("Z")
    assert store.watch_remove("keyword", "ps5") == 1
    assert store.watch_list() == []

    store.cache_categories({"categories": [{"id": "100"}]})
    assert store.cached_categories() == {"categories": [{"id": "100"}]}
    store.cache_metrics("abc", {"views": 3})
    assert store.cached_metrics("abc") == {"views": 3}


def test_purge_drops_old_snapshots(store: Store) -> None:
    store.add_snapshot(
        {"item_id": "old", "captured_at": "2020-01-01T00:00:00Z", "price_amount": 1, "keyword": "viejo"}
    )
    assert store.purge() == 1
    assert store.history(item_id="old") == []
