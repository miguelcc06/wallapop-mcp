"""Métricas reales y ranking por engagement."""

from __future__ import annotations

from fastmcp import FastMCP

from wallapop_intel.context import client
from wallapop_intel.insights import apply_engagement, engagement
from wallapop_intel.models import ItemIdInput, MetricsResponse, RankInput, RankResponse
from wallapop_intel.normalize import to_card
from wallapop_intel.tools.common import bullets, cards_from_search, fail, snapshot_card

_READ = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}
_ORDER = {"low": 0, "medium": 1, "high": 2}


def register(mcp: FastMCP) -> None:
    @mcp.tool(name="wp_item_metrics", title="Métricas de un anuncio", annotations=_READ)
    async def wp_item_metrics(params: ItemIdInput) -> MetricsResponse:
        """Visitas, favoritos y conversaciones de un anuncio ajeno.

        Fuente primaria: counters de GET /api/v3/items/{id} (reales, fiabilidad alta).
        Si counters no viene, el score es heurístico y se marca reliability=low.
        El scraping HTML es opcional y está apagado por defecto: la ficha JSON
        ya coincide con el contador público (verificado 2026-09-24).

        Returns:
            MetricsResponse con la explicación del método.
        """
        try:
            raw = await client().get_item(params.item_id)
            card = to_card(raw)
            images = raw.get("images") or []
            bump = raw.get("bump") if isinstance(raw.get("bump"), dict) else {}
            user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
            score, reliability, method, explanation = engagement(
                card,
                photo_count=len(images) if isinstance(images, list) else 0,
                bumped=bool(bump.get("type") and bump.get("type") != "none"),
                top_profile=bool(user.get("is_top_profile")),
                created_raw=raw.get("modified_date"),
            )
            card = card.model_copy(update={"engagement_score": score, "reliability": reliability, "metrics_method": method})
            snapshot_card(card, None, method, reliability)
            real = method == "api_counters"
            summary = (
                f"# Métricas {card.title} ({card.id})\n\n"
                f"- Visitas: {card.views if real else 'estimadas, no hay contador'}\n"
                f"- Favoritos: {card.favorites if real else 'estimados, no hay contador'}\n"
                f"- Conversaciones: {card.conversations if card.conversations is not None else 'n/d'}\n"
                f"- Score: {score} · fiabilidad {reliability} · {method}\n\n{explanation}"
            )
            return MetricsResponse(
                summary=summary,
                item_id=card.id,
                title=card.title,
                views=card.views,
                favorites=card.favorites,
                conversations=card.conversations,
                views_source="api_counters" if card.views is not None else "unavailable",
                favorites_source="api_counters" if card.favorites is not None else "unavailable",
                engagement_score=score,
                reliability=reliability,
                method=method,
                explanation=explanation,
            )
        except Exception as exc:
            fail(exc)

    @mcp.tool(name="wp_rank_by_engagement", title="Ordenar por visitas o favoritos", annotations=_READ)
    async def wp_rank_by_engagement(params: RankInput) -> RankResponse:
        """Ordena una búsqueda por visitas, favoritos o score de engagement.

        Pide la ficha de los primeros detail_cap anuncios (máx. 12) para usar
        counters reales. El resto se queda con fiabilidad baja y queda fuera
        si min_reliability es high o medium.

        Returns:
            RankResponse ordenado de mayor a menor según metric.
        """
        try:
            payload = await client().search(
                params.keywords,
                latitude=params.latitude,
                longitude=params.longitude,
                min_price=params.min_price,
                max_price=params.max_price,
                category_id=params.category_id,
                order_by=params.order_by.value,
                distance_km=params.distance_km,
                cursor=params.cursor,
            )
            cards, _cursor = cards_from_search(payload, params.timeframe.value, params.limit)
            enriched = []
            for index, card in enumerate(cards):
                if index < params.detail_cap:
                    raw = await client().get_item(card.id)
                    rich = apply_engagement(to_card(raw))
                    snapshot_card(rich, params.keywords, rich.metrics_method, rich.reliability)
                    enriched.append(rich)
                else:
                    enriched.append(card)
            floor = _ORDER.get(params.min_reliability, 0)
            kept = [c for c in enriched if _ORDER.get(c.reliability or "low", 0) >= floor]
            key_name = params.metric.value

            def sort_key(card):
                if key_name == "views":
                    return card.views or -1
                if key_name == "favorites":
                    return card.favorites or -1
                return card.engagement_score or -1

            kept.sort(key=sort_key, reverse=True)
            summary = (
                f"# Ranking «{params.keywords}» por {key_name}\n\n"
                f"Fichas reales consultadas: {min(params.detail_cap, len(cards))}. "
                f"Umbral de fiabilidad: {params.min_reliability}.\n\n"
                + bullets(kept)
            )
            return RankResponse(
                summary=summary,
                metric=key_name,
                count=len(kept),
                items=kept,
                explanation=(
                    "Orden real por counters cuando la ficha los trae. "
                    "engagement = min(100, 8*favoritos + 0.35*visitas + 12*conversaciones)."
                ),
            )
        except Exception as exc:
            fail(exc)
