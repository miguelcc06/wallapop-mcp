"""Id de usuario, errores HTTP y /users/{id}/items con HTTP simulado (sin red)."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from wallapop_intel.client import WallapopClient, WallapopError
from wallapop_intel.config import Settings
from wallapop_intel.normalize import to_card

HASH = "abc123def456"
PROFILE_URL = "https://es.wallapop.com/user/ana-12345678"


def _run(handler, call):
    async def main():
        settings = replace(Settings.from_env(), max_retries=1, jitter_ms=0, proxy=None, sign_requests=False)
        api = WallapopClient(settings)
        api._bucket.min_interval = 0
        api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await call(api)
        finally:
            await api.close()

    return asyncio.run(main())


def _no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"petición inesperada: {request.url}")


def _profile_page(user_id: str = HASH, web_slug: str = "ana-12345678") -> httpx.Response:
    data = {"props": {"pageProps": {"user": {"id": user_id, "webSlug": web_slug}}}}
    return httpx.Response(200, text=f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>')


@pytest.mark.parametrize("value", [HASH, f" {HASH} ", f"https://es.wallapop.com/user/{HASH}"])
def test_hash_needs_no_request(value) -> None:
    assert _run(_no_network, lambda api: api.resolve_user_id(value)) == HASH


@pytest.mark.parametrize(
    "value",
    ["ana-12345678", PROFILE_URL, "es.wallapop.com/user/ana-12345678/reviews", PROFILE_URL + "?_pid=wi"],
)
def test_slug_and_url_resolve_once(value) -> None:
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return _profile_page()

    async def twice(api):
        return await api.resolve_user_id(value), await api.resolve_user_id(value)

    assert _run(handler, twice) == (HASH, HASH)
    assert seen == [PROFILE_URL]


@pytest.mark.parametrize(
    "value",
    ["12345678", "-12345678", "ana", "https://example.com/user/ana-12345678", "https://es.wallapop.com/item/x-1"],
)
def test_unresolvable_values_say_what_to_set(value) -> None:
    with pytest.raises(WallapopError, match="WALLAPOP_USER_ID"):
        _run(_no_network, lambda api: api.resolve_user_id(value))


def test_unknown_slug_is_explained() -> None:
    with pytest.raises(WallapopError, match="slug tiene que ser exacto"):
        _run(lambda r: httpx.Response(404), lambda api: api.resolve_user_id("ana-12345678"))


@pytest.mark.parametrize(
    "response",
    [httpx.Response(200, text="<html></html>"), _profile_page(web_slug="otra-999")],
)
def test_unreadable_or_foreign_profile_page_fails(response) -> None:
    with pytest.raises(WallapopError, match="id hash"):
        _run(lambda r: response, lambda api: api.resolve_user_id("ana-12345678"))


def test_400_without_order_by_does_not_blame_it() -> None:
    with pytest.raises(WallapopError) as info:
        _run(lambda r: httpx.Response(400, text='{"code":"bad id"}'), lambda api: api.get_user_items("12345678"))
    message = str(info.value)
    assert info.value.status_code == 400
    assert "/api/v3/users/12345678/items" in message
    assert '{"code":"bad id"}' in message
    assert "order_by" not in message


def test_400_with_order_by_keeps_hint() -> None:
    with pytest.raises(WallapopError) as info:
        _run(lambda r: httpx.Response(400), lambda api: api.search("silla"))
    message = str(info.value)
    assert "most_relevance" in message
    assert "/api/v3/search" in message
    assert "(cuerpo vacío)" in message


def _row(item_id: str) -> dict:
    return {"id": item_id, "title": f"Anuncio {item_id}", "slug": f"anuncio-{item_id}", "price": {"amount": 15.0, "currency": "EUR"}}


def _pages(request: httpx.Request) -> httpx.Response:
    assert request.url.path == f"/api/v3/users/{HASH}/items"
    since = request.url.params.get("since")
    if since is None:
        return httpx.Response(200, json={"data": [_row("a1"), _row("a2")], "meta": {"next": "CUR"}})
    assert since == "CUR"
    return httpx.Response(200, json={"data": [_row("a3")], "meta": {}})


def test_user_items_follow_since_cursor_and_parse() -> None:
    rows = _run(_pages, lambda api: api.get_user_items(HASH))
    assert [row["id"] for row in rows] == ["a1", "a2", "a3"]
    card = to_card(rows[0])
    assert (card.title, card.price_eur, card.url) == ("Anuncio a1", 15.0, "https://es.wallapop.com/item/anuncio-a1")


def test_user_items_respect_limit() -> None:
    rows = _run(_pages, lambda api: api.get_user_items(HASH, 1))
    assert [row["id"] for row in rows] == ["a1"]


def test_user_items_unexpected_shape_is_an_error() -> None:
    with pytest.raises(WallapopError, match="data"):
        _run(lambda r: httpx.Response(200, json={"items": []}), lambda api: api.get_user_items(HASH))
