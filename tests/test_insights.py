"""Tests locales de heurística y firma. No llaman a Wallapop ni a PostgreSQL."""

from wallapop_intel.client import legacy_x_signature
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


def test_shippable_detection() -> None:
    from wallapop_intel.normalize import to_card

    # Caso en que el vendedor desactiva el envío aunque el producto sea apto
    raw_no_user_shipping = {
        "id": "item1",
        "title": "Impresora 3D",
        "supports_shipping": {"flag": False},
        "shipping": {"item_is_shippable": True, "user_allows_shipping": False},
    }
    assert to_card(raw_no_user_shipping).shippable is False

    # Caso en que el vendedor sí permite envío
    raw_with_shipping = {
        "id": "item2",
        "title": "Impresora 3D con envío",
        "supports_shipping": {"flag": True},
        "shipping": {"item_is_shippable": True, "user_allows_shipping": True},
    }
    assert to_card(raw_with_shipping).shippable is True

