"""Insights del perfil público propio. Requiere WALLAPOP_USER_ID."""

from __future__ import annotations

from fastmcp import FastMCP

from wallapop_intel.client import extract_items
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
    SellerItemsResponse,
)
from wallapop_intel.normalize import age_days, description_of, to_card
from wallapop_intel.tools.common import fail, missing_user, snapshot_card

_READ = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


async def _own_user_id() -> str:
    configured = settings().user_id
    if not configured:
        missing_user()
    return await client().resolve_user_id(configured)


def _candidate(card: ItemCard) -> Candidate:
    return Candidate(card.id, card.title, card.price_eur, card.user_id)


async def _competition(card: ItemCard, user_id: str) -> dict:
    """Muestra filtrada por relevancia: una búsqueda, y otra reducida si no llega."""
    own = _candidate(card)
    found: dict[str, Candidate] = {}
    sample: list[Candidate] = []
    queries = [card.title[:80]]
    short = reduced_query(card.title)
    if short and short.lower() != card.title.lower():
        queries.append(short)
    for query in queries:
        payload = await client().search(query, order_by="most_relevance")
        for row in extract_items(payload):
            other = _candidate(to_card(row))
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


def register(mcp: FastMCP) -> None:
    @mcp.tool(name="wp_my_items", title="Mis anuncios en venta", annotations=_READ)
    async def wp_my_items(params: MyItemsInput) -> SellerItemsResponse:
        """Anuncios publicados de tu perfil público, con métricas y pista de precio.

        Usa WALLAPOP_USER_ID. No inicia sesión. Por cada anuncio (hasta limit)
        lee la ficha y hace 1-2 búsquedas filtradas para situar el precio.

        Returns:
            SellerItemsResponse. El summary incluye muestra, mediana, p25-p75 y consejo.
        """
        try:
            user_id = await _own_user_id()
            rows = await client().get_user_items(user_id, params.limit)
            lines = [f"# Tus anuncios ({user_id})", ""]
            cards = []
            for row in rows:
                raw = await client().get_item(str(row.get("id")))
                card = apply_engagement(to_card(raw))
                snapshot_card(card, None, card.metrics_method, card.reliability)
                market = await _competition(card, user_id)
                tips = listing_tips(None, len(raw.get("images") or []), description_of(raw))
                cards.append(card)
                lines.append(f"## {card.title} ({card.id})")
                lines.append(f"- Precio {card.price_eur} EUR · visitas {card.views} · fav {card.favorites} · conversaciones {card.conversations}")
                lines.extend(f"- {line}" for line in _market_lines(market))
                lines.extend(f"- {tip}" for tip in [market["advice"], *tips])
                lines.append("")
            return SellerItemsResponse(
                summary="\n".join(lines),
                user_id=user_id,
                count=len(cards),
                items=cards,
                note=(
                    "Competencia = búsqueda por relevancia filtrada por tipo y modelo según el título; "
                    "puede incluir variantes. Precios pedidos, no de venta. Perfil público, sin login."
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
            market = await _competition(card, user_id)
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
