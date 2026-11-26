"""wp_my_items completo: paginación, presupuesto, caché y errores (HTTP simulado)."""

import asyncio
import time
from dataclasses import replace

import httpx
import pytest
from fastmcp import Client, FastMCP

from wallapop_intel.client import WallapopClient
from wallapop_intel.config import Settings
from wallapop_intel.context import reset_for_tests
from wallapop_intel.tools import mine

ME = "abc123def456"


class FakeApi:
    """Perfil con `total` anuncios paginados; cada título tiene su propia competencia."""

    def __init__(self, total: int = 3, page_size: int = 10) -> None:
        self.page_size = page_size
        self.rows = [
            {
                "id": f"mio{n:03d}", "title": f"Lampara modelo{n:03d}",
                "price": {"amount": 10.0, "currency": "EUR"}, "images": [{}] * 4,
                "description": "Probada y funcionando, se envía bien protegida en caja.",
            }
            for n in range(total)
        ]
        self.requests: list[str] = []
        # Por clave (ruta de ficha, «search:<consulta>» o cursor): respuesta o espera.
        self.broken: dict[str, httpx.Response] = {}
        self.slow: dict[str, float] = {}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v3")
        params = request.url.params
        if path == "/search":
            key = f"search:{params['keywords']}"
        elif path.endswith("/items") and path.startswith("/users/"):
            key = f"list:{params.get('since', '0')}"
        else:
            key = path
        self.requests.append(key)
        await asyncio.sleep(self.slow.get(key, 0))
        if key in self.broken:
            return self.broken[key]
        if key.startswith("list:"):
            start = int(params.get("since", 0))
            end = start + self.page_size
            meta = {"next": str(end)} if end < len(self.rows) else {}
            return httpx.Response(200, json={"data": self.rows[start:end], "meta": meta})
        if path == "/search":
            title = params["keywords"]
            found = [
                {"id": f"{title}-{price}", "title": title, "user_id": "otro", "price": {"amount": price}}
                for price in (18.0, 20.0, 22.0)
            ]
            return httpx.Response(200, json={"data": {"section": {"payload": {"items": found}}}})
        item_id = path.removeprefix("/items/")
        row = next(row for row in self.rows if row["id"] == item_id)
        return httpx.Response(
            200, json={**row, "user": {"id": ME}, "counters": {"views": 7, "favorites": 2, "conversations": 1}}
        )

    def count(self, prefix: str) -> int:
        return sum(key.startswith(prefix) for key in self.requests)


class _Db:
    def add_snapshot(self, row):
        pass


def _session(api: FakeApi, steps, *, budget: float = 30.0, db=None, retries: int = 1):
    """Ejecuta steps(call) contra un mismo servidor (la caché se conserva)."""

    async def main():
        cfg = replace(
            Settings.from_env(), user_id=ME, items_budget_s=budget, max_retries=retries,
            jitter_ms=0, proxy=None, sign_requests=False,
        )
        client = WallapopClient(cfg)
        client._bucket.min_interval = 0
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(api))
        reset_for_tests(cfg, db or _Db(), client)
        server = FastMCP("test")
        mine.register(server)
        try:
            async with Client(server) as session:

                async def call(tool="wp_my_items", **params):
                    return (await session.call_tool(tool, {"params": params})).structured_content

                return await steps(call)
        finally:
            await client.close()
            reset_for_tests(None, None, None)

    return asyncio.run(main())


def _once(call):
    return call()


async def _twice(call):
    return await call(), await call()


def test_lists_every_item_across_pages() -> None:
    api = FakeApi(total=45, page_size=10)
    out = _session(api, _once)
    assert out["count"] == 45 and len({item["id"] for item in out["items"]}) == 45
    assert out["truncated"] is False
    assert (out["pending_metrics"], out["pending_competition"], out["failed"]) == (0, 0, 0)
    assert "Lista completa: 45 anuncios" in out["summary"]
    assert (api.count("list:"), api.count("/items/"), api.count("search:")) == (5, 45, 45)


def test_all_fichas_come_before_any_search() -> None:
    api = FakeApi(total=4)
    _session(api, _once)
    kinds = [key.split("/")[0] or "ficha" for key in api.requests]
    assert kinds[1:5] == ["ficha"] * 4
    assert all(kind.startswith("search:") for kind in kinds[5:])


def test_limit_is_optional_and_reported() -> None:
    out = _session(FakeApi(total=25), lambda call: call(limit=12))
    assert out["count"] == 12 and out["truncated"] is True
    assert "limit=12" in out["summary"]
    exact = _session(FakeApi(total=12), lambda call: call(limit=12))
    assert exact["count"] == 12 and exact["truncated"] is False


@pytest.mark.parametrize("broken", [httpx.Response(500), httpx.Response(200, json={"x": 1})])
def test_failed_later_page_is_declared(broken) -> None:
    api = FakeApi(total=25)
    api.broken["list:10"] = broken
    out = _session(api, _once)
    assert out["count"] == 10 and out["truncated"] is True
    assert "Lista incompleta" in out["summary"]


def test_stuck_cursor_is_declared() -> None:
    api = FakeApi(total=25)
    api.broken["list:10"] = httpx.Response(200, json={"data": api.rows[:10], "meta": {"next": "10"}})
    out = _session(api, _once)
    assert out["count"] == 10 and "cursor" in out["truncated_reason"]


def test_listing_without_time_fails_clearly() -> None:
    api = FakeApi()
    api.slow["list:0"] = 5
    with pytest.raises(Exception, match="Sin tiempo para leer"):
        _session(api, _once, budget=0.1)


def test_exhausted_budget_keeps_every_item_and_invents_nothing() -> None:
    api = FakeApi(total=30, page_size=30)
    api.slow = {f"/items/mio{n:03d}": 0.05 for n in range(30)}
    started = time.monotonic()
    out = _session(api, _once, budget=0.4)
    assert time.monotonic() - started < 2
    assert out["count"] == 30
    assert 0 < out["pending_metrics"] < 30 and out["pending_competition"] == 30
    last = out["items"][-1]
    assert (last["title"], last["price_eur"]) == ("Lampara modelo029", 10.0)
    assert last["views"] is None and last["engagement_score"] is None
    assert "Precio 10.0 EUR · métricas: pendiente" in out["summary"]
    assert "mediana" not in out["summary"].split("## ")[-1]
    assert f"{out['pending_metrics']} sin métricas y 30 sin comparación" in out["summary"]


def test_repeating_completes_pending_without_repeating_requests() -> None:
    api = FakeApi(total=12, page_size=20)
    api.slow = {f"/items/mio{n:03d}": 0.05 for n in range(12)}

    async def until_done(call):
        runs = [await call()]
        while runs[-1]["pending_competition"] and len(runs) < 10:
            runs.append(await call())
        before = len(api.requests)
        runs.append(await call())
        return runs, api.requests[before:]

    runs, last_requests = _session(api, until_done, budget=0.3)
    assert runs[0]["pending_metrics"] > 0
    assert runs[-1]["pending_metrics"] == runs[-1]["pending_competition"] == 0
    assert last_requests == ["list:0"]
    # Solo puede repetirse la petición que cortó cada llamada.
    assert len(api.requests) - len(set(api.requests)) <= 2 * len(runs)
    assert api.count("search:") == 12


def test_slow_request_does_not_block_the_rest(monkeypatch) -> None:
    monkeypatch.setattr(mine, "OP_TIMEOUT_S", 0.1)
    api = FakeApi()
    api.slow["/items/mio000"] = 30
    started = time.monotonic()
    out = _session(api, _once)
    assert time.monotonic() - started < 2
    assert (out["pending_metrics"], out["pending_competition"]) == (1, 0)
    assert [item["views"] for item in out["items"]] == [None, 7, 7]


def test_slow_request_cannot_exceed_budget() -> None:
    api = FakeApi()
    api.slow["/items/mio000"] = 30
    started = time.monotonic()
    out = _session(api, _once, budget=0.2)
    assert time.monotonic() - started < 2
    assert out["count"] == 3 and out["pending_metrics"] == 3


def test_retry_after_stays_within_budget_and_is_respected() -> None:
    api = FakeApi()
    api.broken["/items/mio000"] = httpx.Response(429, headers={"Retry-After": "120"})
    started = time.monotonic()
    out = _session(api, _once, budget=0.2, retries=3)
    assert time.monotonic() - started < 2
    assert out["count"] == 3 and out["pending_metrics"] == 3
    assert api.requests == ["list:0", "/items/mio000"]


def test_slow_database_stays_within_budget() -> None:
    class SlowDb:
        def add_snapshot(self, row):
            time.sleep(0.6)

    started = time.monotonic()
    out = _session(FakeApi(), _once, budget=0.2, db=SlowDb())
    # asyncio.run espera al hilo huérfano al cerrar; la respuesta ya había salido.
    assert time.monotonic() - started < 1.5
    assert out["count"] == 3


def test_failing_database_does_not_break_the_list() -> None:
    class BrokenDb:
        def add_snapshot(self, row):
            raise RuntimeError("sin base")

    out = _session(FakeApi(), _once, db=BrokenDb())
    assert (out["pending_metrics"], out["failed"]) == (0, 0)


def test_partial_errors_keep_the_list_and_are_retried() -> None:
    api = FakeApi()
    api.broken["/items/mio000"] = httpx.Response(404)
    api.broken["search:Lampara modelo001"] = httpx.Response(403)

    async def steps(call):
        first = await call()
        api.broken.clear()
        return first, await call()

    first, second = _session(api, steps)
    assert first["count"] == 3 and first["failed"] == 2
    assert (first["pending_metrics"], first["pending_competition"]) == (1, 1)
    assert "métricas: error (HTTP 404" in first["summary"]
    assert "Competencia: error (HTTP 403" in first["summary"]
    assert (second["pending_metrics"], second["pending_competition"], second["failed"]) == (0, 0, 0)
    assert api.count("/items/mio002") == 1 and api.count("search:Lampara modelo002") == 1


def test_price_change_repositions_and_title_change_searches_again() -> None:
    api = FakeApi(total=1)

    async def steps(call):
        first = await call()
        api.rows[0]["price"] = {"amount": 50.0, "currency": "EUR"}
        repriced = await call()
        searches = api.count("search:")
        api.rows[0]["title"] = "Silla roja"
        return first, repriced, searches, await call()

    first, repriced, searches, renamed = _session(api, steps)
    assert "Posición de tu precio: por debajo" in first["summary"]
    assert "Posición de tu precio: por encima" in repriced["summary"]
    assert repriced["items"][0]["price_eur"] == 50.0
    assert searches == 1
    assert "search:Silla roja" in api.requests
    assert renamed["items"][0]["title"] == "Silla roja"
    assert api.count("/items/") == 3


def test_caches_expire(monkeypatch) -> None:
    monkeypatch.setattr(mine, "ITEM_TTL_S", 0)
    monkeypatch.setattr(mine, "SEARCH_TTL_S", 0)
    api = FakeApi(total=2)
    _session(api, _twice)
    assert (api.count("/items/"), api.count("search:")) == (4, 4)


def test_advice_works_and_shares_the_search_cache() -> None:
    api = FakeApi(total=1)

    async def steps(call):
        await call()
        return await call("wp_my_item_advice", item_id="mio000")

    out = _session(api, steps)
    assert out["market_median"] == 20.0 and out["views"] == 7 and out["competitor_count"] == 3
    assert api.count("search:") == 1


@pytest.mark.parametrize(("raw", "expected"), [(None, 45.0), ("20", 20.0), ("0", 1.0)])
def test_budget_setting(monkeypatch, raw, expected) -> None:
    if raw is None:
        monkeypatch.delenv("WALLAPOP_ITEMS_BUDGET_S", raising=False)
    else:
        monkeypatch.setenv("WALLAPOP_ITEMS_BUDGET_S", raw)
    assert Settings.from_env().items_budget_s == expected
