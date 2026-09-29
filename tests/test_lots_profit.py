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
