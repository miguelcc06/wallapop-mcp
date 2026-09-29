"""Lotes y margen. No llama a Wallapop."""

import pytest

from wallapop_intel.lots import classify_listing, search_keywords
from wallapop_intel.profit import ProfitError, estimate_profit, protection_ceiling


def test_profit_checkout_passes_on_roi() -> None:
    row = estimate_profit(
        buy_price_eur=100,
        sell_price_eur=150,
        weight_bracket="up_to_2kg",
        shipping_eur=3.5,
        protection_eur=2.5,
    )
    assert row["reliability"] == "high"
    assert row["method"] == "checkout_observed"
    assert row["cost_is_ceiling"] is False
    assert row["acquisition_eur"] == 106.0
    assert row["net_eur"] == 44.0
    assert row["passes_roi"] is True
    assert row["passes"] is True


def test_profit_thresholds_are_exclusive() -> None:
    tied = estimate_profit(
        buy_price_eur=100,
        sell_price_eur=130,
        shipping_eur=0,
        protection_eur=0,
        min_roi=0.30,
        min_net_eur=30,
    )
    assert tied["roi"] == 0.3
    assert tied["net_eur"] == 30
    assert tied["passes_roi"] is False
    assert tied["passes_net"] is False
    assert tied["passes"] is False

    by_net = estimate_profit(
        buy_price_eur=200,
        sell_price_eur=230,
        shipping_eur=10,
        protection_eur=0,
        min_roi=0.30,
        min_net_eur=20,
    )
    assert by_net["passes_roi"] is False
    assert by_net["acquisition_eur"] == 210
    assert by_net["net_eur"] == 20
    assert by_net["passes"] is False

    just_over = estimate_profit(
        buy_price_eur=200,
        sell_price_eur=230.01,
        shipping_eur=10,
        protection_eur=0,
        min_net_eur=20,
        min_roi=0.90,
    )
    assert just_over["passes_net"] is True
    assert just_over["passes_roi"] is False
    assert just_over["passes"] is True


def test_profit_ceiling_when_checkout_missing() -> None:
    row = estimate_profit(buy_price_eur=80, sell_price_eur=140, weight_kg=3)
    assert row["weight_bracket"] == "up_to_5kg"
    assert row["inbound_shipping_eur"] == 8.0
    assert row["inbound_protection_eur"] == protection_ceiling(80)
    assert row["reliability"] == "low"
    assert row["cost_is_ceiling"] is True


def test_profit_rejects_overweight_and_mismatched_bracket() -> None:
    with pytest.raises(ProfitError):
        estimate_profit(buy_price_eur=10, sell_price_eur=40, weight_kg=31)
    with pytest.raises(ProfitError):
        estimate_profit(buy_price_eur=10, sell_price_eur=40, weight_kg=1, weight_bracket="up_to_10kg")


def test_profit_seller_deductions() -> None:
    row = estimate_profit(
        buy_price_eur=50,
        sell_price_eur=90,
        weight_bracket="up_to_2kg",
        shipping_eur=4,
        protection_eur=1.5,
        packaging_eur=2,
        home_pickup_eur=6.99,
        bulky_fee_eur=4.5,
    )
    assert row["seller_deductions_eur"] == 13.49
    assert row["proceeds_eur"] == 76.51
    assert row["acquisition_eur"] == 55.5


def test_lot_keywords_and_classification() -> None:
    assert search_keywords("torre oficina", "lot") == "torre oficina lote"
    assert search_keywords("lote de pcs", "lot") == "lote de pcs"
    assert search_keywords("gpu", "teardown") == "gpu despiece"
    assert search_keywords("ps5", "urgent").endswith("urgente")

    torre = classify_listing("Lote torre oficina i5 16GB")
    assert torre is not None and torre["kind"] == "lot"
    piezas = classify_listing("RTX 3070", "se vende para piezas, no funciona")
    assert piezas is not None and "teardown" in piezas["kinds"]
    urgente = classify_listing("PS5 slim", "urgente por mudanza")
    assert urgente is not None and urgente["kind"] == "urgent"
    mixto = classify_listing("Lote torre oficina", "urgente por mudanza")
    assert mixto is not None and mixto["kind"] == "mixed" and mixto["kinds"] == ["lot", "urgent"]
    assert classify_listing("iPhone 13 128 GB negro") is None
