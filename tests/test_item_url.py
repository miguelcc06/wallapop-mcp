"""wp_item_from_url y resolución de URL de anuncio a id de API (HTTP simulado)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from fastmcp import Client, FastMCP

from wallapop_intel.client import WallapopClient, WallapopError
from wallapop_intel.config import Settings
from wallapop_intel.context import reset_for_tests
from wallapop_intel.tools import catalog, item_url

SLUG = "hp-prodesk-600-g3-mini-lote-3-uds-1234567890"
ITEM_ID = "w67v2xx3896x"
ITEM = {
    "id": ITEM_ID, "title": {"original": "HP ProDesk 600 G3 mini (lote 3 uds)!"},
    "description": {"original": "Pack de 3, sin disco"}, "price": {"amount": 120.0, "currency": "EUR"},
    "web_slug": SLUG, "user": {"id": "nzx5y8xd2562"}, "category_id": 15000,
    "taxonomy": [{"id": 1, "name": "Informática"}, {"id": 2, "name": "Ordenadores"}],
    "location": {"city": "Madrid", "latitude": 40.4, "longitude": -3.7, "postal_code": "28001"},
    "supports_shipping": {"flag": True}, "reserved": {"flag": False},
    "counters": {"views": 40, "favorites": 5, "conversations": 2},
}


def _page(slug: str, item_id: str = ITEM_ID) -> str:
    data = {"props": {"pageProps": {"other": {"id": "zzzzzzzzzzzz"}, "item": {"id": item_id, "slug": slug}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == f"/item/{SLUG}":
        return httpx.Response(200, text=_page(SLUG))
    if request.url.path == "/item/roto":
        return httpx.Response(200, text="<html>sin datos</html>")
    if request.url.path.startswith("/item/"):
        return httpx.Response(404, text="no")
    if request.url.path == f"/api/v3/items/{ITEM_ID}":
        return httpx.Response(200, json=ITEM)
    return httpx.Response(404, json={})


def _api() -> WallapopClient:
    cfg = replace(Settings.from_env(), jitter_ms=0, proxy=None, sign_requests=False, max_retries=1)
    api = WallapopClient(cfg)
    api._bucket.min_interval = 0
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    return api


def test_resolve_item_id_forms() -> None:
    async def go():
        api = _api()
        try:
            assert await api.resolve_item_id(ITEM_ID) == ITEM_ID
            assert await api.resolve_item_id(f"https://es.wallapop.com/item/{SLUG}?utm=x") == ITEM_ID
            assert await api.resolve_item_id(f"es.wallapop.com/item/{SLUG}") == ITEM_ID
            for bad in ("https://evil.com/item/x-1", "https://es.wallapop.com/user/x-1", "no es nada", "a-b"):
                with pytest.raises(WallapopError):
                    await api.resolve_item_id(bad)
            with pytest.raises(WallapopError, match="vendió o se retiró"):
                await api.resolve_item_id("https://es.wallapop.com/item/otro-99")
            with pytest.raises(WallapopError, match="la página cambió"):
                await api.resolve_item_id("https://es.wallapop.com/item/roto")
        finally:
            await api.close()

    asyncio.run(go())


class _Db:
    def add_snapshot(self, row):
        pass


def _call(tool: str, **params):
    async def go():
        api = _api()
        reset_for_tests(api.settings, _Db(), api)
        server = FastMCP("t")
        item_url.register(server)
        catalog.register(server)
        try:
            async with Client(server) as session:
                return (await session.call_tool(tool, {"params": params})).structured_content
        finally:
            await api.close()
            reset_for_tests(None, None, None)

    return asyncio.run(go())


def test_item_from_url_returns_item_and_next_calls() -> None:
    out = _call("wp_item_from_url", url=f"https://es.wallapop.com/item/{SLUG}", resale_price_eur=250)
    assert out["item_id"] == ITEM_ID and out["item"]["price_eur"] == 120.0
    assert out["categories"] == ["Informática", "Ordenadores"]
    assert out["lot_kind"] and out["counters_are_real"]
    calls = out["next_calls"]
    assert calls["wp_market_analysis"] == {
        "keywords": "HP ProDesk 600 G3 mini lote", "category_id": 15000, "latitude": 40.4, "longitude": -3.7}
    assert calls["wp_get_seller"] == {"user_id": "nzx5y8xd2562"}
    assert calls["wp_watchlist_add"]["key"] == ITEM_ID
    assert calls["wp_estimate_profit"]["purchase_price"] == 120.0
    assert calls["wp_estimate_profit"]["expected_resale_price"] == 250
    assert out["profit"]["net_profit"] > 0 and "ROI" in out["summary"]


def test_item_tools_accept_url() -> None:
    out = _call("wp_get_item", item_id=f"https://es.wallapop.com/item/{SLUG}")
    assert out["item"]["id"] == ITEM_ID
