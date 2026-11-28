"""Pipeline de flipping: lotes -> margen -> watchlist en una sola llamada."""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from wallapop_intel.context import store
from wallapop_intel.insights import estimate_resell_profit, weight_kg_from_band
from wallapop_intel.models import FlipDeal, FlipScanInput, FlipScanResponse
from wallapop_intel.tools.common import fail
from wallapop_intel.tools.lots import find_lots

_WRITE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

_CANDIDATES = 20


def register(mcp: FastMCP) -> None:
    @mcp.tool(name="wp_flip_scan", title="Pipeline de flipping", annotations=_WRITE)
    async def wp_flip_scan(params: FlipScanInput) -> FlipScanResponse:
        """Busca lotes (wp_search_lots), calcula el margen de cada uno (wp_estimate_profit) y
        devuelve solo los que superan min_roi_percent. Los que pasan se guardan en la
        watchlist local (wp_watchlist_add) salvo add_to_watchlist=false.

        Pasa resale_price_eur con el precio al que sabes revender (p. ej. un lote de
        HP ProDesk). Sin él se usa la mediana de la muestra como referencia, que es
        un precio de lote/averiado y no de reventa: fiabilidad baja. Con
        alert_target_pct la entrada nace con alerta de precio (wp_watchlist_snapshot).

        Returns:
            FlipScanResponse.deals ordenados por ROI descendente. Solo lectura en Wallapop.
        """
        try:
            query, cards, med, rows = await find_lots(params.model_copy(update={"max_results": _CANDIDATES}))
            resale = params.resale_price_eur or med
            if resale is None:
                raise ToolError(
                    "No hay precio de reventa: la muestra no tiene precios. Pasa resale_price_eur."
                )
            source = "resale_price_eur (indicado)" if params.resale_price_eur else "mediana de la muestra (fiabilidad baja)"
            weight = weight_kg_from_band(params.weight_band.value)
            deals: list[FlipDeal] = []
            for row in rows:
                price = row.item.price_eur
                if price is None or price <= 0:
                    continue
                if params.max_buy_price_eur is not None and price > params.max_buy_price_eur:
                    continue
                calc = estimate_resell_profit(
                    purchase_price=price,
                    expected_resale_price=resale,
                    weight_kg=weight,
                    include_inbound_shipping=params.include_inbound_shipping,
                    other_costs=params.other_costs,
                    weight_band=params.weight_band.value,
                    is_international=params.is_international,
                )
                roi = calc["roi_percent"]
                if roi is None or roi <= params.min_roi_percent:
                    continue
                deals.append(
                    FlipDeal(
                        item=row.item,
                        buy_price_eur=price,
                        resale_price_eur=resale,
                        net_profit_eur=calc["net_profit"],
                        roi_percent=roi,
                        total_acquisition_cost_eur=round(price + calc["inbound_shipping_eur"], 2),
                        opportunity_score=row.opportunity_score,
                        signals=row.signals,
                        why=row.why,
                    )
                )
            deals.sort(key=lambda d: d.roi_percent or 0, reverse=True)
            deals = deals[: params.max_results]
            if params.add_to_watchlist:
                for deal in deals:
                    target = (
                        round(deal.buy_price_eur * (1 - params.alert_target_pct / 100), 2)
                        if params.alert_target_pct
                        else None
                    )
                    store().watch_add(
                        "item",
                        deal.item.id,
                        f"flip ROI {deal.roi_percent:.0f}% · {deal.item.title}"[:160],
                        target,
                    )
                    deal.watched = True
                    deal.target_price = target
            lines = [
                f"- **{d.item.title}** ({d.item.id}) · compra {d.buy_price_eur:.0f} EUR → neto {d.net_profit_eur:.2f} EUR · "
                f"ROI {d.roi_percent:.0f}%" + (" · en watchlist" if d.watched else "")
                for d in deals
            ]
            summary = (
                f"# Flip «{params.keywords}»\n\n"
                f"Muestra {len(cards)} · lotes con señal {len(rows)} · reventa {resale:.0f} EUR ({source}) · "
                f"ROI > {params.min_roi_percent:g}%: **{len(deals)}**.\n\n"
                + ("\n".join(lines) if lines else "_Ningún lote supera el umbral de ROI._")
            )
            return FlipScanResponse(
                summary=summary,
                keywords=params.keywords,
                search_query=query,
                sample_size=len(cards),
                lots_found=len(rows),
                resale_price_eur=round(resale, 2),
                resale_source=source,
                min_roi_percent=params.min_roi_percent,
                deals=deals,
                note=(
                    "ROI estimado con tarifas orientativas de envío y protección. Revisa ficha y fotos antes de comprar; "
                    "confirma envío real en el checkout. La mediana no es un precio de reventa fiable: "
                    "pasa resale_price_eur."
                ),
            )
        except Exception as exc:
            fail(exc)
