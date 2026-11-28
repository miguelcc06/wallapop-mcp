"""Pipeline wp_flip_scan, alertas de precio de la watchlist y tiempo de salida de la muestra."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import psycopg
import pytest
from fastmcp import Client, FastMCP

from wallapop_intel.client import WallapopClient
from wallapop_intel.config import Settings, resolve_database_url
from wallapop_intel.context import reset_for_tests
from wallapop_intel.db import Store, format_ts
from wallapop_intel.selltime import compute_sell_time, kaplan_meier_median
from wallapop_intel.tools import market, pipeline, watch
from wallapop_intel.watchlist import notify_new, run_snapshot

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


class FakeStore:
    """Sustituto en memoria de Store con lo que usan las tools."""

    def __init__(self) -> None:
        self.watch: dict[tuple[str, str], dict] = {}
        self.alerts: set[tuple] = set()
        self.snapshots: list[dict] = []

    def add_snapshot(self, row):
        self.snapshots.append(row)

    def watch_add(self, kind, key, label, target_price=None):
        old = self.watch.get((kind, key), {})
        self.watch[(kind, key)] = {
            "kind": kind, "key": key, "label": label,
            "target_price": target_price if target_price is not None else old.get("target_price"),
        }

    def watch_list(self):
        return list(self.watch.values())

    def watch_set_target(self, kind, key, target):
        if (kind, key) not in self.watch:
            return 0
        self.watch[(kind, key)]["target_price"] = target
        return 1

    def alert_seen(self, kind, key, item_id, price):
        return (kind, key, item_id, price) in self.alerts

    def alert_mark(self, kind, key, item_id, price):
        self.alerts.add((kind, key, item_id, price))

    def keyword_snapshots(self, keyword, since, limit=50000):
        return [r for r in self.snapshots if (r.get("keyword") or "") == keyword.lower()]


def _items(*rows):
    return {"data": {"section": {"payload": {"items": [
        {"id": i, "title": t, "price": {"amount": p}, "user_id": "u", "web_slug": f"s-{i}"} for i, t, p in rows
    ]}}}}


def _api(handler) -> WallapopClient:
    cfg = replace(Settings.from_env(), jitter_ms=0, proxy=None, sign_requests=False, max_retries=1)
    api = WallapopClient(cfg)
    api._bucket.min_interval = 0
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return api


def _run_tools(handler, db, steps, *modules):
    async def main():
        api = _api(handler)
        reset_for_tests(api.settings, db, api)
        server = FastMCP("t")
        for module in modules:
            module.register(server)
        try:
            async with Client(server) as session:
                async def call(tool, **params):
                    return (await session.call_tool(tool, {"params": params})).structured_content
                return await steps(call)
        finally:
            await api.close()
            reset_for_tests(None, None, None)

    return asyncio.run(main())


def _market_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path.removeprefix("/api/v3")
    if path == "/search":
        return httpx.Response(200, json=_items(
            ("a1", "Lote 3 HP ProDesk 600 G3 mini", 90.0),
            ("a2", "Pack HP ProDesk urgente para piezas", 240.0),
            ("a3", "HP ProDesk 600 G3 lote 2 uds", 400.0),
            ("a4", "Monitor Dell 24", 100.0),
        ))
    return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1], "title": "x", "price": {"amount": 1}, "description": ""})


# --- wp_flip_scan -----------------------------------------------------------

def test_flip_scan_returns_only_roi_above_threshold_and_watches_them() -> None:
    db = FakeStore()

    async def steps(call):
        return await call("wp_flip_scan", keywords="hp prodesk", resale_price_eur=200, min_roi_percent=30,
                          alert_target_pct=10, max_results=10)

    out = _run_tools(_market_handler, db, steps, pipeline)
    ids = [d["item"]["id"] for d in out["deals"]]
    assert "a1" in ids and "a3" not in ids  # 400 -> pérdida
    assert all(d["roi_percent"] > 30 for d in out["deals"])
    assert out["deals"] == sorted(out["deals"], key=lambda d: -d["roi_percent"])
    assert out["resale_source"].startswith("resale_price_eur")
    first = next(d for d in out["deals"] if d["item"]["id"] == "a1")
    assert first["watched"] and first["target_price"] == 81.0
    assert db.watch[("item", "a1")]["target_price"] == 81.0
    assert ("item", "a3") not in db.watch


def test_flip_scan_without_watchlist_and_median_fallback() -> None:
    db = FakeStore()

    async def steps(call):
        return await call("wp_flip_scan", keywords="hp prodesk", add_to_watchlist=False, min_roi_percent=-100)

    out = _run_tools(_market_handler, db, steps, pipeline)
    assert out["resale_source"].startswith("mediana")
    assert out["deals"] and db.watch == {}


# --- alertas de precio ------------------------------------------------------

def _watch_handler(prices):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v3")
        if path == "/search":
            return httpx.Response(200, json=_items(*[(i, f"HP ProDesk {i}", p) for i, p in prices["kw"].items()]))
        item_id = path.rsplit("/", 1)[-1]
        if item_id == "boom":
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"id": item_id, "title": "Mini PC", "price": {"amount": prices["item"]}})
    return handler


def test_snapshot_evaluates_targets_and_isolates_failures() -> None:
    db = FakeStore()
    db.watch_add("item", "it1", "mini", 50.0)
    db.watch_add("item", "boom", "roto", 10.0)
    db.watch_add("keyword", "hp prodesk", None, 70.0)
    db.watch_add("keyword", "sin umbral", None)
    prices = {"item": 45.0, "kw": {"k1": 60.0, "k2": 90.0, "k3": 70.0}}

    async def go():
        api = _api(_watch_handler(prices))
        reset_for_tests(api.settings, db, api)
        try:
            return await run_snapshot(api, db)
        finally:
            await api.close()
            reset_for_tests(None, None, None)

    report, alerts = asyncio.run(go())
    assert any(r.get("error") for r in report if r["key"] == "boom")
    got = {(a["key"], a["item_id"]): a for a in alerts}
    assert set(got) == {("it1", "it1"), ("hp prodesk", "k1"), ("hp prodesk", "k3")}  # 70 == umbral cuenta
    assert [a["item_id"] for a in alerts if a["key"] == "hp prodesk"] == ["k1", "k3"]  # más barato primero
    assert all(a["new"] for a in alerts)

    sent: list[str] = []
    assert notify_new(alerts, db, sent.append) == 3 and len(sent) == 3
    report, again = asyncio.run(go())
    assert not any(a["new"] for a in again)
    assert notify_new(again, db, sent.append) == 0 and len(sent) == 3


def test_notify_does_not_mark_when_send_fails() -> None:
    db = FakeStore()
    alert = {"kind": "item", "key": "k", "item_id": "k", "title": "t", "price": 5.0,
             "target_price": 6.0, "url": None, "reserved": False, "new": True}

    def boom(_):
        raise RuntimeError("telegram caído")

    with pytest.raises(RuntimeError):
        notify_new([alert], db, boom)
    assert not db.alert_seen("item", "k", "k", 5.0)


def test_watch_tools_target_roundtrip() -> None:
    db = FakeStore()

    async def steps(call):
        await call("wp_watchlist_add", kind="keyword", key="hp prodesk", target_price=60)
        again = await call("wp_watchlist_add", kind="keyword", key="hp prodesk", label="nuevo")
        listed = await call("wp_watchlist_list")
        cleared = await call("wp_watchlist_set_target", kind="keyword", key="hp prodesk")
        return again, listed, cleared

    again, listed, cleared = _run_tools(_watch_handler({"item": 1, "kw": {}}), db, steps, watch)
    assert again["entries"][0]["target_price"] == 60  # re-añadir sin umbral lo conserva
    assert "alerta ≤ 60 EUR" in listed["summary"]
    assert cleared["entries"][0]["target_price"] is None


# --- tiempo de salida -------------------------------------------------------

def _rows(spec):
    """spec: {item: [horas en las que aparece]}; pasadas cada 12 h."""
    return [
        {"item_id": item, "captured_at": format_ts(T0 + timedelta(hours=h)), "title": item,
         "price_amount": 10.0, "reserved": False}
        for item, hours in spec.items() for h in hours
    ]


def test_sell_time_counts_gone_active_and_single() -> None:
    runs = [0, 12, 24, 36, 48]
    rows = _rows({
        "gone24": [0, 12, 24],       # última vez en la pasada 2 de 4 -> desaparecido, 1 día
        "gone12": [0, 12],           # desaparecido, 0,5 días
        "active": runs,              # sigue
        "newish": [36, 48],          # visto en las últimas pasadas
        "once": [0],                 # una sola vez, desaparecido sin duración
    })
    out = compute_sell_time(rows, min_absent_runs=2)
    assert out["runs"] == 5
    assert out["gone"] == 2 and out["single_sighting"] == 1 and out["active"] == 2
    assert out["median_days_gone"] == 0.75
    assert out["confidence"] == "very_low"
    assert out["km_median_days"] == 1.0  # 4 muestras: tras el 2.º evento S=0,5
    gone = {i["item_id"] for i in out["items"] if i["status"] == "gone"}
    assert gone == {"gone24", "gone12"}


def test_sell_time_min_absent_runs_and_empty() -> None:
    rows = _rows({"x": [0, 12, 24], "y": [0, 12, 24, 36, 48]})
    assert compute_sell_time(rows, min_absent_runs=2)["gone"] == 1
    assert compute_sell_time(rows, min_absent_runs=3)["gone"] == 0
    empty = compute_sell_time([])
    assert empty["runs"] == 0 and empty["median_days_gone"] is None


def test_kaplan_meier_median_censoring() -> None:
    assert kaplan_meier_median([(1, False), (2, False)]) is None
    assert kaplan_meier_median([(1, True), (2, True), (3, False)]) == 2


def test_sell_time_tool_needs_runs() -> None:
    db = FakeStore()

    async def steps(call):
        return await call("wp_sell_time", keywords="hp prodesk")

    out = _run_tools(_market_handler, db, steps, market)
    assert out["runs"] == 0 and "hacen falta" in out["summary"]


# --- Postgres real ----------------------------------------------------------

@pytest.fixture
def pg_store():
    dsn = resolve_database_url()
    try:
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception:
        pytest.skip("PostgreSQL no disponible")
    db = Store(dsn, ttl_days=90, schema=f"wp_test_{uuid.uuid4().hex[:12]}", min_size=1, max_size=2)
    try:
        yield db
    finally:
        db.drop_schema()


def test_db_target_alerts_and_keyword_snapshots(pg_store: Store) -> None:
    pg_store.watch_add("item", "abc1", "uno", 50.0)
    pg_store.watch_add("item", "abc1", "dos")  # conserva el umbral
    assert pg_store.watch_list()[0]["target_price"] == 50.0
    assert pg_store.watch_set_target("item", "abc1", 40.0) == 1
    assert pg_store.watch_set_target("item", "zzz9", 40.0) == 0
    assert pg_store.watch_set_target("item", "abc1", None) == 1
    assert pg_store.watch_list()[0]["target_price"] is None

    assert not pg_store.alert_seen("item", "abc1", "abc1", 39.0)
    pg_store.alert_mark("item", "abc1", "abc1", 39.0)
    pg_store.alert_mark("item", "abc1", "abc1", 39.0)
    assert pg_store.alert_seen("item", "abc1", "abc1", 39.0)
    assert not pg_store.alert_seen("item", "abc1", "abc1", 35.0)  # otro precio vuelve a avisar

    now = datetime.now(timezone.utc)
    for n, hours in enumerate((30, 20, 10)):
        pg_store.add_snapshot({"item_id": "s1", "captured_at": format_ts(now - timedelta(hours=hours)),
                               "keyword": "HP ProDesk", "title": "t", "price_amount": 10 + n})
    pg_store.add_snapshot({"item_id": "viejo", "captured_at": format_ts(now - timedelta(days=60)), "keyword": "hp prodesk"})
    rows = pg_store.keyword_snapshots("hp prodesk", now - timedelta(days=7))
    assert [r["price_amount"] for r in rows] == [10, 11, 12]
    assert rows[0]["captured_at"].endswith("Z")
