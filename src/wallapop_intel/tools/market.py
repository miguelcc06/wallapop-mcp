"""Análisis de muestra, histórico local, oportunidades y ganadores."""

from __future__ import annotations

from fastmcp import FastMCP

from wallapop_intel.client import WallapopError
from wallapop_intel.context import client, store
from wallapop_intel.db import utcnow
from wallapop_intel.insights import opportunities, price_stats, winners
from wallapop_intel.models import (
    MarketInput,
    MarketResponse,
    OpportunitiesResponse,
    OpportunityInput,
    PriceHistoryInput,
    PriceHistoryResponse,
    WinnersResponse,
)
from wallapop_intel.tools.common import bullets, cards_from_search, fail, snapshot_card

_READ = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


def register(mcp: FastMCP) -> None:
    @mcp.tool(name="wp_market_analysis", title="Análisis de mercado", annotations=_READ)
    async def wp_market_analysis(params: MarketInput) -> MarketResponse:
        """Radiografía una keyword: volumen de la muestra, precios y cambios vs histórico local.

        Guarda un snapshot. Compara con snapshots previos de la misma keyword:
        bajadas de precio y anuncios que ya no salen en la muestra (posible venta
        o salida; no es un hecho confirmado).

        Returns:
            MarketResponse. El volumen no es el total de Wallapop, es la muestra pedida.
        """
        try:
            cards = await _sample(params)
            stats = price_stats(cards)
            now = utcnow()
            previous = store().previous_ids(params.keywords, now)
            current_ids = {c.id for c in cards}
            drops = []
            for card in cards:
                old = previous.get(card.id)
                if not old or old.get("price_amount") is None or card.price_eur is None:
                    continue
                if card.price_eur < float(old["price_amount"]) - 0.5:
                    drops.append(
                        {
                            "item_id": card.id,
                            "title": card.title,
                            "from_price": old["price_amount"],
                            "to_price": card.price_eur,
                            "captured_before": old["captured_at"],
                        }
                    )
            disappeared = []
            for item_id, old in previous.items():
                if item_id not in current_ids:
                    disappeared.append(
                        {
                            "item_id": item_id,
                            "title": old.get("title"),
                            "last_price": old.get("price_amount"),
                            "last_seen": old.get("captured_at"),
                            "confidence": "low",
                            "reason": "No está en la muestra actual. Puede haberse vendido, reservado fuera de página o cambiado de título.",
                        }
                    )
            summary = (
                f"# Mercado «{params.keywords}»\n\n"
                f"Muestra de {len(cards)} anuncios. "
                f"Precio mín {stats['min']} · mediana {stats['median']} · máx {stats['max']} EUR.\n"
                f"Bajadas detectadas: {len(drops)}. Ausentes respecto al histórico: {len(disappeared[:8])}.\n\n"
                + bullets(cards[:8])
            )
            return MarketResponse(
                summary=summary,
                keywords=params.keywords,
                sample_size=len(cards),
                min_price=_r(stats["min"]),
                median_price=_r(stats["median"]),
                max_price=_r(stats["max"]),
                p25=_r(stats["p25"]),
                p75=_r(stats["p75"]),
                newest_per_day_estimate=None,
                price_drops=drops[:10],
                disappeared=disappeared[:10],
                note="Sin snapshots previos, price_drops y disappeared salen vacíos. Vuelve a llamar más tarde.",
            )
        except Exception as exc:
            fail(exc)

    @mcp.tool(name="wp_price_history", title="Histórico de precio", annotations=_READ)
    async def wp_price_history(params: PriceHistoryInput) -> PriceHistoryResponse:
        """Evolución de precio guardada en SQLite para un anuncio o una keyword.

        Si aún no hay filas y pasas item_id, toma un snapshot ahora y lo indica.
        No inventa precios anteriores a la primera captura.

        Returns:
            PriceHistoryResponse.points ordenados del más reciente al más antiguo.
        """
        try:
            if not params.item_id and not params.keywords:
                raise WallapopError("Indica item_id o keywords. Ejemplo: item_id='w67v2xx3896x'.")
            points = store().history(item_id=params.item_id, keyword=params.keywords, limit=params.limit)
            if not points and params.item_id:
                raw_cards = await _one_item_snapshot(params.item_id)
                points = store().history(item_id=params.item_id, limit=params.limit)
                note = "Primera captura hecha ahora. El histórico crecerá en llamadas futuras."
                _ = raw_cards
            else:
                note = "Puntos locales. Una fila por anuncio y minuto."
            summary = "# Histórico\n\n" + "\n".join(
                f"- {p['captured_at']}: {p.get('title')} {p.get('price_amount')} {p.get('currency') or ''} "
                f"(visitas {p.get('views')}, fav {p.get('favorites')}, {p.get('reliability')})"
                for p in points[:20]
            )
            return PriceHistoryResponse(summary=summary or "# Histórico\n\nSin puntos.", points=points, note=note)
        except Exception as exc:
            fail(exc)

    @mcp.tool(name="wp_opportunities", title="Oportunidades de precio", annotations=_READ)
    async def wp_opportunities(params: OpportunityInput) -> OpportunitiesResponse:
        """Candidatos con precio claramente bajo la mediana de la misma búsqueda.

        Umbral: precio <= mediana * 0.78. No confirma ganga: el estado y la
        descripción hay que leerlos en la ficha. Score más alto = más descuento.

        Returns:
            OpportunitiesResponse.
        """
        try:
            cards = await _sample(params)
            med, rows = opportunities(cards, params.max_results)
            summary = f"# Oportunidades «{params.keywords}»\n\nMediana {med} EUR.\n\n" + "\n".join(
                f"- {row.item.title} ({row.item.id}) a {row.item.price_eur} EUR · score {row.score} · {row.why}"
                for row in rows
            )
            return OpportunitiesResponse(
                summary=summary or "Sin oportunidades bajo el umbral.",
                median_price=med,
                opportunities=rows,
                note="Heurística de precio sobre la muestra, no sobre todo Wallapop. Fiabilidad media.",
            )
        except Exception as exc:
            fail(exc)

    @mcp.tool(name="wp_winning_products", title="Productos ganadores", annotations=_READ)
    async def wp_winning_products(params: MarketInput) -> WinnersResponse:
        """Agrupa la búsqueda y puntúa clusters con demanda y dispersión de precio.

        Pide ficha de los 6 primeros para mezclar favoritos/visitas reales.
        Un grupo necesita al menos 3 anuncios. El score es engagement_v1,
        no una predicción de venta.

        Returns:
            WinnersResponse.
        """
        try:
            cards = await _sample(params, detail_cap=6)
            rows = winners(cards)
            summary = f"# Ganadores en «{params.keywords}»\n\n" + "\n".join(
                f"- {row.label}: score {row.score} · n={row.sample_size} · mediana {row.median_price} · {row.why}"
                for row in rows
            )
            return WinnersResponse(
                summary=summary or "Ningún grupo alcanza 3 anuncios en la muestra.",
                winners=rows,
                method="engagement_v1 cluster por 3 tokens del título",
                note="Si la muestra es heterogénea (p. ej. 'ps5' mezcla consola y juegos), afina la keyword.",
            )
        except Exception as exc:
            fail(exc)


async def _sample(params: MarketInput, detail_cap: int = 0):
    payload = await client().search(
        params.keywords,
        latitude=params.latitude,
        longitude=params.longitude,
        category_id=params.category_id,
        distance_km=params.distance_km,
        order_by="newest",
    )
    cards, _ = cards_from_search(payload, "any", params.limit)
    if detail_cap:
        from wallapop_intel.insights import apply_engagement
        from wallapop_intel.normalize import to_card

        enriched = []
        for index, card in enumerate(cards):
            if index < detail_cap:
                raw = await client().get_item(card.id)
                rich = apply_engagement(to_card(raw))
                snapshot_card(rich, params.keywords, rich.metrics_method, rich.reliability)
                enriched.append(rich)
            else:
                snapshot_card(card, params.keywords, "search_listing", "medium")
                enriched.append(card)
        return enriched
    for card in cards:
        snapshot_card(card, params.keywords, "search_listing", "medium")
    return cards


async def _one_item_snapshot(item_id: str):
    from wallapop_intel.insights import apply_engagement
    from wallapop_intel.normalize import to_card

    raw = await client().get_item(item_id)
    card = apply_engagement(to_card(raw))
    snapshot_card(card, None, card.metrics_method, card.reliability)
    return card


def _r(value):
    return round(value, 2) if isinstance(value, float) else value
