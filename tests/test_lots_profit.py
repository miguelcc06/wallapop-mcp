"""Tests de lotes, rentabilidad y envíos. No llaman a Wallapop."""

from wallapop_intel.insights import (
    buyer_protection_fee,
    estimate_resell_profit,
    lot_opportunities,
    lot_signals,
    shipping_cost_eur,
    signal_score_from_text,
    weight_kg_from_band,
)
from wallapop_intel.models import ItemCard


def test_lot_signals_detect_keywords() -> None:
    hits = lot_signals("Lote de 3 móviles para piezas urge vender")
    labels = {label for label, _ in hits}
    assert "lote" in labels
    assert "para piezas" in labels
    assert "urge" in labels


def test_signal_score_from_title() -> None:
    score, labels = signal_score_from_text("Pack consolas averiado no enciende")
    assert score > 20
    assert "pack" in labels
    assert "no enciende" in labels


def test_lot_opportunities_ranks_cheaper_lot() -> None:
    cards = [
        ItemCard(id="a", title="Lote juegos ps4", price_eur=40),
        ItemCard(id="b", title="PS4 slim negro", price_eur=180),
        ItemCard(id="c", title="Pack 2 mandos para piezas", price_eur=25),
    ]
    med, rows = lot_opportunities(cards, limit=5, min_score=1.0)
    assert med == 40.0
    assert rows
    assert rows[0].item.id in {"a", "c"}
    assert rows[0].opportunity_score >= rows[-1].opportunity_score


def test_lot_opportunities_skips_non_lot_titles() -> None:
    cards = [ItemCard(id="x", title="iPhone 13 perfecto estado", price_eur=300)]
    _, rows = lot_opportunities(cards, min_score=0)
    assert rows == []


def test_shipping_tiers_conservative() -> None:
    low, _ = shipping_cost_eur(1.0, conservative=False)
    high, _ = shipping_cost_eur(1.0, conservative=True)
    assert low == 2.95
    assert high == 3.95
    mid, _ = shipping_cost_eur(4.0, conservative=True)
    assert mid == 4.95
    heavy, _ = shipping_cost_eur(8.0, conservative=True)
    assert heavy == 7.95


def test_buyer_protection_fee() -> None:
    assert buyer_protection_fee(100.0) == 7.50


def test_estimate_resell_profit_roi() -> None:
    result = estimate_resell_profit(
        purchase_price=50.0,
        expected_resale_price=100.0,
        weight_kg=3.5,
        include_outbound_shipping=True,
        include_buyer_protection=True,
        conservative_shipping=True,
    )
    assert result["outbound_shipping_eur"] == 4.95
    assert result["buyer_protection_eur"] == 7.50
    assert result["gross_margin"] == 50.0
    assert result["net_profit"] == round(50.0 - 4.95 - 7.50, 2)
    assert result["roi_percent"] == round(result["net_profit"] / 50.0 * 100, 2)


def test_weight_kg_from_band() -> None:
    assert weight_kg_from_band("kg_5_10") == 7.5


def test_international_shipping_tiers() -> None:
    low, _ = shipping_cost_eur(1.0, conservative=False, is_international=True)
    high, _ = shipping_cost_eur(1.0, conservative=True, is_international=True)
    assert low == 5.95
    assert high == 6.95

    mid_low, _ = shipping_cost_eur(4.0, conservative=False, is_international=True)
    mid_high, _ = shipping_cost_eur(4.0, conservative=True, is_international=True)
    assert mid_low == 7.95
    assert mid_high == 8.95

    ten, _ = shipping_cost_eur(8.0, conservative=True, is_international=True)
    assert ten == 11.95
    twenty, _ = shipping_cost_eur(15.0, conservative=False, is_international=True)
    assert twenty == 16.95
    over, _ = shipping_cost_eur(25.0, conservative=True, is_international=True)
    assert over == 24.95

    domestic, _ = shipping_cost_eur(1.0, conservative=True, is_international=False)
    assert domestic == 3.95


def test_estimate_resell_profit_international_exact() -> None:
    result = estimate_resell_profit(
        purchase_price=50.0,
        expected_resale_price=100.0,
        weight_kg=1.0,
        include_outbound_shipping=True,
        include_inbound_shipping=True,
        include_buyer_protection=True,
        other_costs=1.0,
        conservative_shipping=True,
        is_international=True,
    )
    outbound = 6.95
    inbound = 6.95
    protection = 7.50
    other = 1.0
    gross = 50.0
    total = round(outbound + inbound + protection + other, 2)
    net = round(gross - total, 2)
    assert result["is_international"] is True
    assert result["outbound_shipping_eur"] == outbound
    assert result["inbound_shipping_eur"] == inbound
    assert result["buyer_protection_eur"] == protection
    assert result["other_costs"] == other
    assert result["gross_margin"] == gross
    assert result["total_costs"] == total
    assert result["net_profit"] == net
    assert result["net_profit"] == 27.60
    assert result["roi_percent"] == round(net / 50.0 * 100, 2)


def test_estimate_resell_profit_international_low_tier() -> None:
    result = estimate_resell_profit(
        purchase_price=80.0,
        expected_resale_price=120.0,
        weight_kg=4.0,
        include_outbound_shipping=True,
        include_buyer_protection=False,
        conservative_shipping=False,
        is_international=True,
    )
    assert result["outbound_shipping_eur"] == 7.95
    assert result["buyer_protection_eur"] == 0.0
    assert result["gross_margin"] == 40.0
    assert result["net_profit"] == round(40.0 - 7.95, 2)
    assert result["roi_percent"] == round(result["net_profit"] / 80.0 * 100, 2)
    assert result["is_international"] is True
