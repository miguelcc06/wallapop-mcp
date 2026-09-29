"""Calculadora de rentabilidad de reventa con envíos y protección comprador."""

from __future__ import annotations

from fastmcp import FastMCP

from wallapop_intel.client import WallapopError
from wallapop_intel.insights import estimate_resell_profit, weight_kg_from_band
from wallapop_intel.models import EstimateProfitInput, ProfitEstimateResponse
from wallapop_intel.tools.common import fail

_READ = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


def register(mcp: FastMCP) -> None:
    @mcp.tool(name="wp_estimate_profit", title="Estimar beneficio de reventa", annotations=_READ)
    async def wp_estimate_profit(params: EstimateProfitInput) -> ProfitEstimateResponse:
        """Calcula margen bruto, costes estimados y beneficio neto al revender en Wallapop.

        Resta envío Wallapop Envíos según peso (tramos <2 kg, 2-5, 5-10, 10-20, >20).
        Con is_international=true usa las tarifas transfronterizas (p. ej. Portugal o Italia).
        Resta también la protección comprador (~2.50 EUR + 5% del precio de venta) y otros gastos.
        Beneficio neto = margen bruto − costes. ROI = beneficio neto / precio de compra.

        Indica weight_kg o weight_band. Sin ambos, asume tramo under_2kg (1.5 kg).

        Returns:
            ProfitEstimateResponse con desglose y supuestos documentados.
        """
        try:
            if params.weight_kg is not None:
                weight = params.weight_kg
                band_label = params.weight_band.value if params.weight_band else None
            elif params.weight_band is not None:
                weight = weight_kg_from_band(params.weight_band.value)
                band_label = params.weight_band.value
            else:
                weight = weight_kg_from_band("under_2kg")
                band_label = "under_2kg"
            if params.expected_resale_price < params.purchase_price:
                note = "Precio de reventa por debajo de la compra: el ROI será negativo salvo ajustes."
            else:
                note = "Estimación local; no incluye impuestos ni comisiones bancarias."
            result = estimate_resell_profit(
                purchase_price=params.purchase_price,
                expected_resale_price=params.expected_resale_price,
                weight_kg=weight,
                include_outbound_shipping=params.include_outbound_shipping,
                include_inbound_shipping=params.include_inbound_shipping,
                include_buyer_protection=params.include_buyer_protection,
                other_costs=params.other_costs,
                conservative_shipping=params.conservative_shipping,
                weight_band=band_label,
                is_international=params.is_international,
            )
            roi = result["roi_percent"]
            roi_line = f"{roi:.1f}%" if roi is not None else "n/d (compra a 0 EUR)"
            shipping_scope = "internacional" if params.is_international else "nacional"
            summary = (
                f"# Rentabilidad reventa\n\n"
                f"| Concepto | EUR |\n| --- | ---: |\n"
                f"| Compra | {params.purchase_price:.2f} |\n"
                f"| Coste de adquisición | {result['total_acquisition_cost']:.2f} |\n"
                f"| Venta esperada | {params.expected_resale_price:.2f} |\n"
                f"| Margen bruto | {result['gross_margin']:.2f} |\n"
                f"| Envío venta ({shipping_scope}) | {result['outbound_shipping_eur']:.2f} |\n"
                f"| Envío compra ({shipping_scope}) | {result['inbound_shipping_eur']:.2f} |\n"
                f"| Protección comprador | {result['buyer_protection_eur']:.2f} |\n"
                f"| Otros | {result['other_costs']:.2f} |\n"
                f"| Costes totales | {result['total_costs']:.2f} |\n"
                f"| **Beneficio neto** | **{result['net_profit']:.2f}** |\n"
                f"| **ROI** | **{roi_line}** |\n\n"
                f"Peso usado: {result['weight_kg_used']} kg ({band_label or 'por peso'}). "
                f"Envío {shipping_scope}.\n\n"
                + "\n".join(f"- {a}" for a in result["assumptions"])
            )
            return ProfitEstimateResponse(
                summary=summary,
                purchase_price=round(params.purchase_price, 2),
                expected_resale_price=round(params.expected_resale_price, 2),
                gross_margin=result["gross_margin"],
                net_profit=result["net_profit"],
                roi_percent=result["roi_percent"],
                outbound_shipping_eur=result["outbound_shipping_eur"],
                inbound_shipping_eur=result["inbound_shipping_eur"],
                buyer_protection_eur=result["buyer_protection_eur"],
                other_costs=result["other_costs"],
                total_costs=result["total_costs"],
                total_acquisition_cost=result["total_acquisition_cost"],
                is_international=params.is_international,
                weight_kg_used=result["weight_kg_used"],
                weight_band=band_label,
                is_international=result["is_international"],
                assumptions=result["assumptions"],
                note=note,
            )
        except WallapopError as exc:
            fail(exc)
        except Exception as exc:
            fail(exc)
