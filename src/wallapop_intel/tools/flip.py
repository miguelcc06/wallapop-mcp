"""Lotes y margen neto. Solo lectura: no compra ni publica."""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from wallapop_intel.client import extract_cursor, extract_items
from wallapop_intel.context import client
from wallapop_intel.insights import apply_engagement
from wallapop_intel.lots import classify_listing, search_keywords
from wallapop_intel.models import LotHit, LotsInput, LotsResponse, ProfitInput, ProfitResponse
from wallapop_intel.normalize import description_of, title_of, to_card, within_timeframe
from wallapop_intel.profit import ProfitError, estimate_profit
from wallapop_intel.tools.common import fail, snapshot_card

_READ = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}
_LOCAL = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


def register(mcp: FastMCP) -> None:
    @mcp.tool(name="wp_search_lots", title="Buscar lotes y despieces", annotations=_READ)
    async def wp_search_lots(params: LotsInput) -> LotsResponse:
        """Busca packs, torres, despieces o urgencias y clasifica la muestra.

        Añade un sufijo (lote, pack, despiece, urgente) si la keyword no lo trae.
        La clasificación es local sobre título y descripción: no es un filtro
        de la API. Un anuncio sin esas palabras no entra aunque sea un lote.

        Returns:
            LotsResponse.hits con kind, señales y la ficha. next_cursor pagina.
        """
        try:
            keywords = search_keywords(params.keywords, params.focus.value)
            payload = await client().search(
                keywords,
                latitude=params.latitude,
                longitude=params.longitude,
                min_price=params.min_price,
                max_price=params.max_price,
                category_id=params.category_id,
                order_by=params.order_by.value,
                distance_km=params.distance_km,
                cursor=params.cursor,
            )
            hits: list[LotHit] = []
            scanned = 0
            for raw in extract_items(payload):
                if not within_timeframe(raw, params.timeframe.value):
                    continue
                scanned += 1
                card = apply_engagement(to_card(raw))
                snapshot_card(card, keywords, "search_listing", "medium")
                classified = classify_listing(title_of(raw), description_of(raw))
                if classified is None:
                    continue
                if params.focus.value != "any" and params.focus.value not in classified["kinds"]:
                    continue
                hits.append(
                    LotHit(
                        item=card,
                        kind=classified["kind"],
                        kinds=classified["kinds"],
                        signals=classified["signals"],
                        why=(
                            f"Señales {', '.join(classified['signals'])} en título o descripción. "
                            "La tool no tasa componentes: eso es una búsqueda por pieza."
                        ),
                    )
                )
            matched = len(hits)
            truncated = matched > params.limit
            hits = hits[: params.limit]
            summary = (
                f"# Lotes «{keywords}» ({params.focus.value})\n\n"
                f"Revisados {scanned}. Con señal: {matched}.\n\n"
                + "\n".join(
                    f"- [{hit.kind}] {hit.item.title} ({hit.item.id}) — "
                    f"{hit.item.price_eur if hit.item.price_eur is not None else 's/p'} EUR · "
                    f"{', '.join(hit.signals)}"
                    for hit in hits
                )
            )
            return LotsResponse(
                summary=summary or "Sin lotes con esas señales.",
                keywords_used=keywords,
                focus=params.focus.value,
                scanned=scanned,
                count=len(hits),
                hits=hits,
                next_cursor=extract_cursor(payload),
                note=(
                    "Solo anuncios cuyo texto contiene la señal. Un lote mal titulado no aparece. "
                    "reserved y shippable siguen en la ficha: no se descartan aquí. "
                    + ("limit ha recortado la página." if truncated else "La página cabe en limit.")
                ),
            )
        except Exception as exc:
            fail(exc)

    @mcp.tool(name="wp_estimate_profit", title="Margen neto de reventa", annotations=_LOCAL)
    async def wp_estimate_profit(params: ProfitInput) -> ProfitResponse:
        """Coste de compra (precio + envío + protección) y beneficio de la reventa.

        Pasa shipping_eur y protection_eur vistos en el checkout para fiabilidad
        alta. Si faltan, usa un techo de referencia no oficial (fiabilidad baja):
        aprobar con ese techo es conservador; suspender puede ser un falso negativo.
        La puerta pasa si el ROI es mayor que min_roi (0,30) o el neto es mayor
        que min_net_eur (20). Igualar el umbral no pasa.

        Returns:
            ProfitResponse. No llama a Wallapop.
        """
        try:
            row = estimate_profit(
                buy_price_eur=params.buy_price_eur,
                sell_price_eur=params.sell_price_eur,
                weight_kg=params.weight_kg,
                weight_bracket=params.weight_bracket.value if params.weight_bracket else None,
                shipping_eur=params.shipping_eur,
                protection_eur=params.protection_eur,
                packaging_eur=params.packaging_eur,
                home_pickup_eur=params.home_pickup_eur,
                bulky_fee_eur=params.bulky_fee_eur,
                min_roi=params.min_roi,
                min_net_eur=params.min_net_eur,
            )
            roi_txt = "n/d" if row["roi"] is None else f"{row['roi']:.1%}"
            verdict = "PASA" if row["passes"] else "NO PASA"
            summary = (
                f"# Margen {verdict}\n\n"
                f"- Compra {params.buy_price_eur:.2f} + envío {row['inbound_shipping_eur']:.2f} "
                f"+ protección {row['inbound_protection_eur']:.2f} = coste {row['acquisition_eur']:.2f} EUR\n"
                f"- Venta {params.sell_price_eur:.2f} − deducciones {row['seller_deductions_eur']:.2f} "
                f"= ingreso {row['proceeds_eur']:.2f} EUR\n"
                f"- Neto {row['net_eur']:.2f} EUR · ROI {roi_txt} · fiabilidad {row['reliability']} ({row['method']})\n"
                f"- Puerta: ROI > {params.min_roi:.0%} ({'sí' if row['passes_roi'] else 'no'}) "
                f"o neto > {params.min_net_eur:.0f} EUR ({'sí' if row['passes_net'] else 'no'})."
            )
            return ProfitResponse(
                summary=summary,
                buy_price_eur=params.buy_price_eur,
                sell_price_eur=params.sell_price_eur,
                note=(
                    "El techo de envío y de protección no es una tarifa de Wallapop. "
                    "Sustitúyelo por el checkout antes de comprar si reliability es low y el resultado no pasa."
                ),
                **row,
            )
        except ProfitError as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            fail(exc)

