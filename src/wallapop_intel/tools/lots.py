"""Búsqueda orientada a lotes, packs y oportunidades de piezas/averiados."""

from __future__ import annotations

from fastmcp import FastMCP

from wallapop_intel.context import client
from wallapop_intel.insights import lot_opportunities
from wallapop_intel.models import SearchLotsInput, SearchLotsResponse
from wallapop_intel.normalize import description_of
from wallapop_intel.tools.common import fail
from wallapop_intel.tools.market import _sample

_READ = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

_LOT_QUERY_SUFFIX = "lote pack piezas"


def _build_query(keywords: str, append: bool) -> str:
    base = keywords.strip()
    if not append:
        return base
    lower = base.lower()
    if any(tok in lower for tok in ("lote", "pack", "piezas", "averiado", "urge", "no enciende")):
        return base
    return f"{base} {_LOT_QUERY_SUFFIX}"


def register(mcp: FastMCP) -> None:
    @mcp.tool(name="wp_search_lots", title="Buscar lotes y oportunidades", annotations=_READ)
    async def wp_search_lots(params: SearchLotsInput) -> SearchLotsResponse:
        """Búsqueda especializada para detectar packs, lotes, «para piezas», averiados y urgencias.

        Combina la muestra de Wallapop con señales en el título (y descripción de los
        primeros anuncios enriquecidos). Calcula signal_score, price_score, urgency_score
        y un opportunity_score compuesto. No confirma el estado real del producto.

        Returns:
            SearchLotsResponse con métricas por anuncio y mediana de la muestra.
        """
        try:
            query = _build_query(params.keywords, params.append_lot_keywords)
            sample_params = params.model_copy(update={"keywords": query})
            cards = await _sample(sample_params, detail_cap=0)
            descriptions: dict[str, str] = {}
            for card in cards[:6]:
                try:
                    raw = await client().get_item(card.id)
                    descriptions[card.id] = description_of(raw) or ""
                except Exception:
                    descriptions[card.id] = ""

            med, rows = lot_opportunities(
                cards,
                limit=params.max_results,
                min_score=params.min_opportunity_score,
                descriptions=descriptions,
            )
            summary = (
                f"# Lotes y oportunidades «{params.keywords}»\n\n"
                f"Consulta API: `{query}`. Muestra {len(cards)} · mediana {med} EUR.\n\n"
                + "\n".join(
                    f"- **{row.item.title}** ({row.item.id}) · {row.item.price_eur or 's/p'} EUR · "
                    f"score **{row.opportunity_score}** · señales [{', '.join(row.signals)}]\n"
                    f"  signal {row.signal_score} · precio {row.price_score} · urgencia {row.urgency_score}\n"
                    f"  {row.why}"
                    for row in rows
                )
            )
            if not rows:
                summary += "\n\n_Sin anuncios que superen el umbral de señales/score en esta muestra._"
            return SearchLotsResponse(
                summary=summary,
                keywords=params.keywords,
                search_query=query,
                sample_size=len(cards),
                median_price=med,
                lots=rows,
                note=(
                    "Heurística sobre título/descripción y precios de la muestra. "
                    "Revisa fotos y ficha antes de comprar. Tarifas de reventa: wp_estimate_profit."
                ),
            )
        except Exception as exc:
            fail(exc)
