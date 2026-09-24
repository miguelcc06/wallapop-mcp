"""Insights del perfil público propio. Requiere WALLAPOP_USER_ID."""

from __future__ import annotations

from fastmcp import FastMCP

from wallapop_intel.context import client, settings
from wallapop_intel.insights import apply_engagement, price_advice, price_stats
from wallapop_intel.models import AdviceResponse, MyAdviceInput, MyItemsInput, SellerItemsResponse
from wallapop_intel.normalize import age_days, description_of, to_card
from wallapop_intel.tools.common import bullets, cards_from_search, fail, missing_user, snapshot_card

_READ = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


def register(mcp: FastMCP) -> None:
    @mcp.tool(name="wp_my_items", title="Mis anuncios en venta", annotations=_READ)
    async def wp_my_items(params: MyItemsInput) -> SellerItemsResponse:
        """Anuncios publicados de tu perfil público, con métricas y pista de precio.

        Usa WALLAPOP_USER_ID. No inicia sesión. Por cada anuncio (hasta limit)
        lee la ficha y una búsqueda corta del título para situar el precio.

        Returns:
            SellerItemsResponse. El summary incluye la sugerencia de cada uno.
        """
        try:
            user_id = settings().user_id
            if not user_id:
                missing_user()
            rows = await client().get_user_items(user_id)
            lines = [f"# Tus anuncios ({user_id})", ""]
            cards = []
            for row in rows[: params.limit]:
                raw = await client().get_item(str(row.get("id")))
                card = apply_engagement(to_card(raw))
                snapshot_card(card, None, card.metrics_method, card.reliability)
                market = await client().search(card.title[:80], order_by="price_low_to_high")
                sample, _ = cards_from_search(market, "any", 15)
                med = price_stats(sample)["median"]
                tips = price_advice(card.price_eur, med, None, len(raw.get("images") or []), description_of(raw))
                cards.append(card)
                lines.append(f"## {card.title} ({card.id})")
                lines.append(f"- Precio {card.price_eur} EUR · visitas {card.views} · fav {card.favorites}")
                lines.append(f"- Mediana de competencia (muestra {len(sample)}): {med}")
                lines.append(f"- {tips[0] if tips else ''}")
                lines.append("")
            return SellerItemsResponse(
                summary="\n".join(lines),
                user_id=user_id,
                count=len(cards),
                items=cards,
                note="Competencia = búsqueda por el título, no un match exacto de modelo. Perfil público, sin login.",
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
            user_id = settings().user_id
            if not user_id:
                missing_user()
            raw = await client().get_item(params.item_id)
            owner = (raw.get("user") or {}).get("id")
            if owner and owner != user_id:
                from fastmcp.exceptions import ToolError

                raise ToolError(
                    f"El anuncio {params.item_id} pertenece a {owner}, no a WALLAPOP_USER_ID ({user_id}). "
                    "Usa wp_item_metrics para anuncios ajenos."
                )
            card = apply_engagement(to_card(raw))
            market = await client().search(card.title[:80], order_by="newest")
            sample, _ = cards_from_search(market, "any", 20)
            sample = [c for c in sample if c.id != card.id]
            med = price_stats(sample)["median"]
            created = raw.get("modified_date")
            days = age_days(created if created and created > 10_000_000_000 else (created * 1000 if isinstance(created, (int, float)) else None))
            photos = len(raw.get("images") or [])
            desc = description_of(raw)
            tips = price_advice(card.price_eur, med, days, photos, desc)
            summary = (
                f"# Consejo para {card.title}\n\n"
                + "\n".join(f"- {tip}" for tip in tips)
                + f"\n\nCompetidores en la muestra: {len(sample)}."
            )
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
                competitor_count=len(sample),
            )
        except Exception as exc:
            fail(exc)
