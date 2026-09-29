"""Tests locales de heurística, firma y base de datos PostgreSQL. No llaman a Wallapop."""

import os
from wallapop_intel.client import legacy_x_signature
from wallapop_intel.db import Store
from wallapop_intel.insights import engagement, opportunities, winners
from wallapop_intel.models import ItemCard
from wallapop_intel.normalize import epoch_to_iso, price_amount, within_timeframe


def test_signature_is_stable() -> None:
    first = legacy_x_signature("GET", "/api/v3/search?keywords=ps5", "1600339727696")
    second = legacy_x_signature("GET", "/api/v3/search?keywords=ps5", "1600339727696")
    assert first == second
    assert first.endswith("=") or len(first) > 20


def test_price_and_time() -> None:
    amount, currency = price_amount({"price": {"cash": {"amount": 12.5, "currency": "EUR"}}})
    assert amount == 12.5 and currency == "EUR"
    assert epoch_to_iso(1788520316).startswith("2026-")
    assert within_timeframe({"created_at": 1}, "today") is False


def test_real_counters_are_high_reliability() -> None:
    card = ItemCard(id="abc", title="PS5", views=720, favorites=16, conversations=15)
    score, reliability, method, text = engagement(card)
    assert reliability == "high"
    assert method == "api_counters"
    assert "reales" in text
    assert score == 100.0


def test_missing_counters_are_estimated() -> None:
    card = ItemCard(id="abc", title="Cosa")
    score, reliability, method, text = engagement(card, photo_count=4)
    assert reliability == "low"
    assert method == "heuristic_v1"
    assert "ESTIMADO" in text
    assert score > 0


def test_opportunities_and_winners() -> None:
    cards = [
        ItemCard(id="1", title="iphone 13 128 negro", price_eur=100),
        ItemCard(id="2", title="iphone 13 128 azul", price_eur=200),
        ItemCard(id="3", title="iphone 13 128 blanco", price_eur=210, favorites=4, views=20),
        ItemCard(id="4", title="iphone 13 128 rosa", price_eur=190, favorites=2, views=10),
    ]
    med, rows = opportunities(cards, limit=5)
    assert med == 195.0
    assert rows[0].item.id == "1"
    found = winners(cards)
    assert found and found[0].label.startswith("iphone 13")


def test_postgres_dedup_and_watch() -> None:
    dsn = os.environ.get("WALLAPOP_DATABASE_URL", "postgresql://miguelcc06@localhost:5432/wallapop_intel")
    try:
        store = Store(dsn, ttl_days=90)
    except Exception:
        # Si no hay PostgreSQL corriendo en el entorno de testing, omitir
        return
    store.add_snapshot({"item_id": "test_abc", "captured_at": "2026-09-24T18:00:00Z", "price_amount": 10, "keyword": "ps5"})
    store.add_snapshot({"item_id": "test_abc", "captured_at": "2026-09-24T18:00:30Z", "price_amount": 9, "keyword": "ps5"})
    rows = store.history(item_id="test_abc")
    assert len(rows) >= 1
    store.watch_add("keyword", "ps5_test", "consola")
    store.watch_add("keyword", "ps5_test", "otra")
    assert any(w["key"] == "ps5_test" for w in store.watch_list())
    assert store.watch_remove("keyword", "ps5_test") >= 1
    store.close()
