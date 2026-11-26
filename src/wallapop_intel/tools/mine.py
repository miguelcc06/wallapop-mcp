"""Insights del perfil público propio. Requiere WALLAPOP_USER_ID.

wp_my_items trabaja contra un presupuesto de tiempo: listado, después la ficha
de todos los anuncios y después la competencia. Lo que no cabe queda pendiente.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import FastMCP

from wallapop_intel.client import WallapopError, extract_items
from wallapop_intel.competition import (
    MIN_SAMPLE,
    Candidate,
    filter_competitors,
    reduced_query,
    summarize,
)
from wallapop_intel.context import client, settings
from wallapop_intel.insights import apply_engagement, listing_tips
from wallapop_intel.models import (
    AdviceResponse,
    ItemCard,
    MyAdviceInput,
    MyItemsInput,
    MyItemsResponse,
)
from wallapop_intel.normalize import age_days, description_of, to_card
from wallapop_intel.tools.common import fail, missing_user, snapshot_card

_READ = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

# La ficha caduca pronto (visitas); la búsqueda de competencia aguanta más.
ITEM_TTL_S = 5 * 60
SEARCH_TTL_S = 20 * 60
# Tope por operación, para que un anuncio lento no se coma el presupuesto de los demás.
OP_TIMEOUT_S = 10.0
SNAPSHOT_TIMEOUT_S = 3.0
_PENDING = "pendiente"


class TtlCache:
    def __init__(self, ttl_s: float) -> None:
        self.ttl_s = ttl_s
        self._rows: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any:
        row = self._rows.get(key)
        if row is None or row[0] <= time.monotonic():
            self._rows.pop(key, None)
            return None
        return row[1]

    def put(self, key: Any, value: Any) -> None:
        now = time.monotonic()
        self._rows = {k: row for k, row in self._rows.items() if row[0] > now}
        self._rows[key] = (now + self.ttl_s, value)


class Budget:
    """Tiempo total de una llamada. Sin segundos, no hay límite."""

    def __init__(self, seconds: float | None = None) -> None:
        self.deadline = None if seconds is None else time.monotonic() + seconds

    def remaining(self) -> float | None:
        return None if self.deadline is None else max(0.0, self.deadline - time.monotonic())

    async def run(self, call: Callable[[], Awaitable[Any]], cap: float | None = None) -> Any:
        """Ejecuta call() con el tiempo restante (y cap); TimeoutError si no cabe."""
        left = self.remaining()
        if left is None:
            return await call()
        if left == 0:
            raise TimeoutError
        return await asyncio.wait_for(call(), left if cap is None else min(left, cap))


def _short(exc: Exception) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text if len(text) <= 160 else f"{text[:157]}..."


async def _own_user_id() -> str:
    configured = settings().user_id
    if not configured:
        missing_user()
    return await client().resolve_user_id(configured)


def _candidate(card: ItemCard) -> Candidate:
    return Candidate(card.id, card.title, card.price_eur, card.user_id)


async def _competition(card: ItemCard, user_id: str, searches: TtlCache, budget: Budget) -> dict:
    """Muestra filtrada por relevancia: una búsqueda, y otra reducida si no llega.

    Se cachean los resultados por consulta, no el veredicto: un cambio de título
    pide otra búsqueda y un cambio de precio se vuelve a situar en la muestra.
    """
    own = _candidate(card)
    found: dict[str, Candidate] = {}
    sample: list[Candidate] = []
    queries = [card.title[:80]]
    short = reduced_query(card.title)
    if short and short.lower() != card.title.lower():
        queries.append(short)
    for query in queries:
        rows = searches.get(query)
        if rows is None:
            payload = await budget.run(
                lambda: client().search(query, order_by="most_relevance"), OP_TIMEOUT_S
            )
            rows = [_candidate(to_card(row)) for row in extract_items(payload)]
            searches.put(query, rows)
        for other in rows:
            found.setdefault(other.id, other)
        sample = filter_competitors(own, list(found.values()), user_id)
        if len(sample) >= MIN_SAMPLE:
            break
    return summarize(card.price_eur, sample)


def _market_lines(market: dict) -> list[str]:
    if market["median"] is None:
        return [f"Muestra comparable: {market['count']} (insuficiente)"]
    lines = [
        f"Muestra comparable: {market['count']} · mediana {market['median']:.2f} EUR · "
        f"p25-p75 {market['p25']:.2f}-{market['p75']:.2f} EUR"
    ]
    if market["position"]:
        lines.append(f"Posición de tu precio: {market['position']}")
    return lines


def _with_ficha(card: ItemCard, raw: dict[str, Any]) -> ItemCard:
    """Añade a la fila del listado lo que solo trae la ficha. Título y precio no se tocan."""
    full = apply_engagement(to_card(raw))
    extra = {
        name: value
        for name, value in full.model_dump(exclude={"id", "title", "price_eur", "currency"}).items()
        if value is not None
    }
    return card.model_copy(update=extra)


async def _snapshot(card: ItemCard, budget: Budget) -> bool:
    """Guarda el histórico sin bloquear el bucle. False si la base no responde."""
    try:
        await budget.run(
            lambda: asyncio.to_thread(
                snapshot_card, card, None, card.metrics_method, card.reliability
            ),
            SNAPSHOT_TIMEOUT_S,
        )
    except Exception:
        return False
    return True


def _item_lines(
    card: ItemCard, row: dict[str, Any], raw: dict | None, market: dict | None, problems: dict[str, str]
) -> list[str]:
    price = "s/p" if card.price_eur is None else f"{card.price_eur} EUR"
    lines = [f"## {card.title} ({card.id})"]
    if raw is None:
        lines.append(f"- Precio {price} · métricas: {problems['ficha']}")
    else:
        lines.append(
            f"- Precio {price} · visitas {card.views} · fav {card.favorites} · "
            f"conversaciones {card.conversations}"
        )
    if market is None:
        lines.append(f"- Competencia: {problems['competencia']}")
        tips = []
    else:
        lines.extend(f"- {line}" for line in _market_lines(market))
        tips = [market["advice"]]
    # Fotos y texto vienen en el listado; la ficha solo cubre lo que falte.
    source = {**(raw or {}), **row}
    if "images" in source and "description" in source:
        tips += listing_tips(None, len(source["images"] or []), description_of(source))
    return [*lines, *(f"- {tip}" for tip in tips), ""]


def register(mcp: FastMCP) -> None:
    fichas = TtlCache(ITEM_TTL_S)
    searches = TtlCache(SEARCH_TTL_S)

    @mcp.tool(name="wp_my_items", title="Mis anuncios en venta", annotations=_READ)
    async def wp_my_items(params: MyItemsInput) -> MyItemsResponse:
        """Todos los anuncios publicados de tu perfil público, con métricas y pista de precio.

        Usa WALLAPOP_USER_ID. No inicia sesión. Lee el listado, después la ficha de
        cada anuncio y después hace 1-2 búsquedas filtradas por anuncio, a 1 req/s
        y dentro de WALLAPOP_ITEMS_BUDGET_S. Lo que no cabe sale marcado como
        pendiente: vuelve a llamar para completarlo.

        Returns:
            MyItemsResponse. El summary incluye muestra, mediana, p25-p75 y consejo,
            y cuántos anuncios quedan pendientes.
        """
        try:
            budget = Budget(settings().items_budget_s)
            try:
                user_id = await budget.run(_own_user_id)
            except TimeoutError:
                raise WallapopError("Sin tiempo para identificar tu usuario. Reintenta.") from None
            listing = await client().list_user_items(user_id, params.limit, budget.remaining())
            rows = {str(row.get("id")): row for row in listing.rows}
            cards = {item_id: to_card(row).model_copy(update={"user_id": user_id}) for item_id, row in rows.items()}
            raws: dict[str, dict] = {}
            markets: dict[str, dict] = {}
            problems: dict[str, dict[str, str]] = {item_id: {} for item_id in rows}
            failed: set[str] = set()
            snapshots_ok = True

            for item_id, card in cards.items():
                # Con otro título o precio en el listado, la ficha guardada ya no vale.
                key = (item_id, card.title, card.price_eur)
                raw = fichas.get(key)
                fresh = raw is None
                try:
                    if fresh:
                        raw = await budget.run(lambda: client().get_item(item_id), OP_TIMEOUT_S)
                        fichas.put(key, raw)
                    cards[item_id] = _with_ficha(card, raw)
                    raws[item_id] = raw
                except TimeoutError:
                    problems[item_id]["ficha"] = _PENDING
                    continue
                except Exception as exc:
                    problems[item_id]["ficha"] = f"error ({_short(exc)})"
                    failed.add(item_id)
                    continue
                if fresh and snapshots_ok:
                    snapshots_ok = await _snapshot(cards[item_id], budget)

            for item_id, card in cards.items():
                try:
                    markets[item_id] = await _competition(card, user_id, searches, budget)
                except TimeoutError:
                    problems[item_id]["competencia"] = _PENDING
                except Exception as exc:
                    problems[item_id]["competencia"] = f"error ({_short(exc)})"
                    failed.add(item_id)

            pending_metrics = len(rows) - len(raws)
            pending_competition = len(rows) - len(markets)
            lines = [f"# Tus anuncios ({user_id})", ""]
            if listing.truncated:
                lines.append(f"**Lista incompleta** ({len(rows)} anuncios): {listing.truncated}.")
            else:
                lines.append(f"Lista completa: {len(rows)} anuncios publicados.")
            if pending_metrics or pending_competition:
                lines.append(
                    f"Sin completar: {pending_metrics} sin métricas y {pending_competition} sin comparación "
                    f"de precio ({len(failed)} con error). Vuelve a llamar a wp_my_items para completarlos; "
                    "lo ya obtenido se reutiliza unos minutos."
                )
            lines.append("")
            for item_id, card in cards.items():
                lines.extend(
                    _item_lines(card, rows[item_id], raws.get(item_id), markets.get(item_id), problems[item_id])
                )
            return MyItemsResponse(
                summary="\n".join(lines),
                user_id=user_id,
                count=len(cards),
                items=list(cards.values()),
                pending_metrics=pending_metrics,
                pending_competition=pending_competition,
                failed=len(failed),
                truncated=listing.truncated is not None,
                truncated_reason=listing.truncated,
                note=(
                    "Competencia = búsqueda por relevancia filtrada por tipo y modelo según el título; "
                    "puede incluir variantes. Precios pedidos, no de venta. Perfil público, sin login. "
                    f"Visitas con hasta {ITEM_TTL_S // 60} min de caché; competencia hasta {SEARCH_TTL_S // 60} min."
                ),
            )
        except Exception as exc:
            fail(exc)

    @mcp.tool(name="wp_my_item_advice", title="Consejo para un anuncio propio", annotations=_READ)
    async def wp_my_item_advice(params: MyAdviceInput) -> AdviceResponse:
        """Diagnóstico de un anuncio tuyo: precio vs muestra, antigüedad, fotos y texto.

        Comprueba que el anuncio pertenezca a WALLAPOP_USER_ID. Si no, lo dice
        y no lo trata como tuyo.

        Returns:
            AdviceResponse con recomendaciones concretas.
        """
        try:
            user_id = await _own_user_id()
            raw = await client().get_item(params.item_id)
            owner = (raw.get("user") or {}).get("id")
            if owner and owner != user_id:
                from fastmcp.exceptions import ToolError

                raise ToolError(
                    f"El anuncio {params.item_id} pertenece a {owner}, no a WALLAPOP_USER_ID ({user_id}). "
                    "Usa wp_item_metrics para anuncios ajenos."
                )
            card = apply_engagement(to_card(raw))
            market = await _competition(card, user_id, searches, Budget())
            created = raw.get("modified_date")
            days = age_days(created if created and created > 10_000_000_000 else (created * 1000 if isinstance(created, (int, float)) else None))
            photos = len(raw.get("images") or [])
            desc = description_of(raw)
            tips = [market["advice"], *listing_tips(days, photos, desc)]
            summary = (
                f"# Consejo para {card.title}\n\n"
                + "\n".join(f"- {line}" for line in [*_market_lines(market), *tips])
            )
            med = market["median"]
            return AdviceResponse(
                summary=summary,
                item_id=card.id,
                your_price=card.price_eur,
                market_median=round(med, 2) if med else None,
                age_days=round(days, 1) if days is not None else None,
                views=card.views,
                favorites=card.favorites,
                photo_count=photos,
                recommendations=tips,
                competitor_count=market["count"],
            )
        except Exception as exc:
            fail(exc)
