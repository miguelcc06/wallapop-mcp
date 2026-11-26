"""Filtro de competencia y consejo de las tools de anuncios propios (sin red)."""

import asyncio

import pytest
from fastmcp import Client, FastMCP

from wallapop_intel.competition import Candidate, filter_competitors, product_type, reduced_query, summarize
from wallapop_intel.config import Settings
from wallapop_intel.context import reset_for_tests
from wallapop_intel.tools import mine

ME = "abc123def456"
PSU = "Fuente de alimentación HP ProDesk 600 G2"
CPU = "Procesador Intel Core i5-6500 LGA1151"


def _c(item_id: str, title: str, price: float | None = 20.0, user: str = "otro") -> Candidate:
    return Candidate(item_id, title, price, user)


def _kept(own_title: str, *titles: str) -> list[str]:
    rows = [_c(str(n), title) for n, title in enumerate(titles)]
    return [c.title for c in filter_competitors(_c("mio1", own_title, user=ME), rows, ME)]


@pytest.mark.parametrize(
    ("title", "kind"),
    [
        (PSU, "fuente"),
        (CPU, "cpu"),
        ("CPU Intel Core i5-6500", "cpu"),
        ("Intel Core i5-6500 LGA 1151", "cpu"),
        ("Procesador Intel i5 6500 + Disipador", "cpu"),
        ("HP ProDesk 600 G3 i5 7500 16GB RAM", "pc"),
        ("HP ProDesk 705 G4 AMD A6-9500", "pc"),
        ("Ordenador HP i5-6600 8GB DDR4 256GB SSD", "pc"),
        ("PC con fuente nueva", "pc"),
        ("Placa Base MSI B150M PRO-VDH + Intel Core I5 6500", "placa"),
        ("Lector DVD Interno SATA HP para PC", "lector"),
        ("Fuente Alimentación HP EliteDesk 800 G4 SFF", "fuente"),
    ],
)
def test_product_type(title, kind) -> None:
    assert product_type(title) == kind


def test_psu_search_drops_computers() -> None:
    good = "Fuente Alimentación HP Z230 240W"
    assert _kept(
        PSU, good, "PC HP ProDesk 600 G2 i5-6500", "Hp prodesk 600 g2",
        "Ordenador HP sobremesa", "HP ProDesk 600 G2 SFF i5-6500 8GB SSD 240GB",
    ) == [good]


def test_cpu_search_drops_boards_computers_lots_and_other_models() -> None:
    good = "Intel Core i5-6500 3.2GHz"
    assert _kept(
        CPU, good, "Placa Base MSI + i5-6500", "PC i5-6500 8GB", "Lote 3 Procesadores i5-6500",
        "Pack ASUS B150M + i5-6500", "Intel i5-6500 caja Nox, fuente y placa", "Procesador i5-6500T",
    ) == [good]


def test_broken_listings_only_compare_with_broken_ones() -> None:
    broken = ["Fuente HP averiada", "Fuente HP para piezas", "Fuente HP piezas o reparar", "Fuente HP no funciona"]
    assert _kept(PSU, *broken) == []
    assert _kept("Fuente HP averiada", *broken) == broken


def test_lot_is_kept_when_own_listing_is_a_lot() -> None:
    assert _kept("Lote procesadores i5-6500", "Lote 6 procesadores i5-6500") == ["Lote 6 procesadores i5-6500"]


def test_pc_requires_same_model_and_generation() -> None:
    good = "HP ProDesk 600 G3 SFF i5 8GB"
    assert _kept(
        "HP ProDesk 600 G3 i5 7500 16GB RAM", good, "HP ProDesk 600 G2 i5 8GB",
        "HP ProDesk 600 G3 i7 16GB", "HP ProDesk i5 Gaming GTX 1050 16GB RAM",
    ) == [good]


def test_incomplete_computers_are_excluded() -> None:
    good = "HP ProDesk 600 G3 SFF i5"
    assert _kept("HP ProDesk 600 G3 i5 16GB", good, "HP ProDesk 600 G3 i5-7500 | Sin RAM/SSD") == [good]


def test_motherboard_matches_by_model() -> None:
    good = "Placa base Spitfire"
    assert _kept("Placa base HP SPITFIRE 795231-001", good, "Placa base HP ProDesk 400") == [good]


def test_own_item_own_user_and_missing_price_are_excluded() -> None:
    own = _c("mio1", PSU, user=ME)
    rows = [own, _c("mio1", PSU), _c("mine", PSU, user=ME), _c("free", PSU, None), _c("ok", PSU)]
    assert [c.id for c in filter_competitors(own, rows, ME)] == ["ok"]


def test_price_outliers_are_dropped() -> None:
    rows = [_c(str(p), PSU, p) for p in (4.0, 18.0, 20.0, 22.0, 90.0)]
    assert [c.price for c in filter_competitors(_c("mio1", PSU), rows, ME)] == [18.0, 20.0, 22.0]


@pytest.mark.parametrize(
    ("price", "position", "text"),
    [(10.0, "por debajo", "subir"), (20.0, "en rango", "en rango"), (40.0, "por encima", "Baja")],
)
def test_advice_follows_position(price, position, text) -> None:
    result = summarize(price, [_c(str(p), PSU, p) for p in (18.0, 20.0, 22.0)])
    assert (result["count"], result["median"], result["p25"], result["p75"]) == (3, 20.0, 19.0, 21.0)
    assert result["position"] == position
    assert text in result["advice"]


def test_short_sample_gives_no_price_verdict() -> None:
    result = summarize(20.0, [_c("1", PSU), _c("2", PSU)])
    assert result["median"] is None and result["position"] is None
    assert "No hay muestra comparable" in result["advice"]
    assert "rango" not in result["advice"]


def test_reduced_query_keeps_brand_and_model() -> None:
    assert reduced_query("HP ProDesk 600 G3 i5 7500 16GB RAM") == "hp prodesk 600 g3"
    assert reduced_query("Placa base HP SPITFIRE 795231-001") == "placa base hp spitfire"
    assert reduced_query(CPU) == "procesador i5 6500"


def _raw(item_id: str, title: str, price: float, user: str) -> dict:
    return {
        "id": item_id, "title": title, "user_id": user, "user": {"id": user},
        "price": {"amount": price, "currency": "EUR"},
        "images": [{}] * 4, "description": "Probada y funcionando, se envía bien protegida en caja.",
    }


class _Api:
    def __init__(self, results: list[dict]) -> None:
        self.results = results
        self.searches: list[tuple[str, str]] = []

    async def resolve_user_id(self, value):
        return ME

    async def get_user_items(self, user_id, limit):
        return [{"id": "mio1"}]

    async def get_item(self, item_id):
        return _raw("mio1", PSU, 15.0, ME)

    async def search(self, keywords, *, order_by):
        self.searches.append((keywords, order_by))
        return {"data": {"section": {"payload": {"items": self.results}}}}


class _Db:
    def add_snapshot(self, row):
        pass


def _call(api: _Api, tool: str, params: dict) -> dict:
    cfg = Settings.from_env()
    object.__setattr__(cfg, "user_id", ME)
    reset_for_tests(cfg, _Db(), api)
    server = FastMCP("test")
    mine.register(server)

    async def main():
        async with Client(server) as session:
            return (await session.call_tool(tool, {"params": params})).structured_content

    try:
        return asyncio.run(main())
    finally:
        reset_for_tests(None, None, None)


@pytest.mark.parametrize(("tool", "params"), [("wp_my_items", {}), ("wp_my_item_advice", {"item_id": "mio1"})])
def test_tools_report_filtered_sample(tool, params) -> None:
    api = _Api([_raw(str(p), PSU, p, "otro") for p in (18.0, 20.0, 22.0)] + [_raw("pc", "PC HP", 120.0, "otro")])
    summary = _call(api, tool, params)["summary"]
    assert "Muestra comparable: 3 · mediana 20.00 EUR · p25-p75 19.00-21.00 EUR" in summary
    assert "Posición de tu precio: por debajo" in summary
    assert api.searches == [(PSU, "most_relevance")]


@pytest.mark.parametrize(("tool", "params"), [("wp_my_items", {}), ("wp_my_item_advice", {"item_id": "mio1"})])
def test_tools_without_sample_search_twice_and_say_so(tool, params) -> None:
    api = _Api([_raw("1", PSU, 18.0, "otro")])
    summary = _call(api, tool, params)["summary"]
    assert "Muestra comparable: 1 (insuficiente)" in summary
    assert "No hay muestra comparable" in summary
    assert "rango" not in summary and "urgente" not in summary
    assert [order for _, order in api.searches] == ["most_relevance"] * 2
