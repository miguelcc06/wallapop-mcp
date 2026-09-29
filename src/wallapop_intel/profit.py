"""Margen neto de una reventa en Wallapop.

Wallapop no publica el precio de cada tramo de peso ni el importe de la
Protección Wallapop (centro de ayuda, 2026). Si el llamante no pasa esas
cifras vistas en el checkout, se aplica un techo conservador
(`reference_ceiling_v1`, fiabilidad baja). Un aprobado con techo es prudente:
el coste real no debería ser mayor. Un suspenso con techo puede ser falso.

Lo que sí está publicado y esta función no inventa:
- el vendedor particular no paga comisión de venta;
- el comprador paga envío y protección;
- si no se elige peso, se factura el tramo de hasta 5 kg;
- tramos: hasta 2 / 5 / 10 / 20 / 30 kg;
- el recargo de voluminoso (más de 30 kg o 120 cm) es 4,50 EUR y lo paga el vendedor.
"""

from __future__ import annotations

BRACKETS: tuple[tuple[str, float, float], ...] = (
    ("up_to_2kg", 2.0, 5.00),
    ("up_to_5kg", 5.0, 8.00),
    ("up_to_10kg", 10.0, 12.00),
    ("up_to_20kg", 20.0, 18.00),
    ("up_to_30kg", 30.0, 26.00),
)

BULKY_PUBLISHED_EUR = 4.50
DEFAULT_BRACKET = "up_to_5kg"


class ProfitError(ValueError):
    """Entrada que no se puede tasar sin inventar un dato."""


def money(value: float) -> float:
    return round(value + 1e-9, 2)


def bracket_for_kg(weight_kg: float) -> str:
    if weight_kg <= 0:
        raise ProfitError("weight_kg tiene que ser mayor que 0.")
    for name, limit, _ceiling in BRACKETS:
        if weight_kg <= limit:
            return name
    raise ProfitError(
        f"{weight_kg:.1f} kg supera los 30 kg del envío estándar. "
        f"Es voluminoso: pasa bulky_fee_eur={BULKY_PUBLISHED_EUR:.2f} y no uses un tramo de peso."
    )


def shipping_ceiling(bracket: str) -> float:
    for name, _limit, ceiling in BRACKETS:
        if name == bracket:
            return ceiling
    raise ProfitError(
        f"Tramo '{bracket}' desconocido. Usa uno de: {', '.join(name for name, _, _ in BRACKETS)}."
    )


def protection_ceiling(price_eur: float) -> float:
    """Techo no oficial: 6 % del precio, entre 1,49 y 25 EUR.

    Sustituye por el importe del checkout. No es la tarifa de Wallapop.
    """
    if price_eur < 0:
        raise ProfitError("El precio de la protección no puede calcularse sobre un importe negativo.")
    return money(min(25.0, max(1.49, price_eur * 0.06)))


def resolve_bracket(weight_kg: float | None, weight_bracket: str | None) -> tuple[str, str]:
    if weight_kg is not None:
        name = bracket_for_kg(weight_kg)
        if weight_bracket and weight_bracket != name:
            raise ProfitError(
                f"weight_kg={weight_kg} cae en {name}, pero weight_bracket es {weight_bracket}. "
                "Pasa solo uno de los dos, o hazlos coincidir."
            )
        return name, f"Tramo {name} deducido de {weight_kg:.2f} kg."
    if weight_bracket:
        shipping_ceiling(weight_bracket)
        return weight_bracket, f"Tramo {weight_bracket} indicado por el llamante."
    return DEFAULT_BRACKET, (
        "Sin peso ni tramo: se usa hasta 5 kg, que es la tarifa que Wallapop factura "
        "si el anuncio no tiene peso elegido."
    )


def estimate_profit(
    *,
    buy_price_eur: float,
    sell_price_eur: float,
    weight_kg: float | None = None,
    weight_bracket: str | None = None,
    shipping_eur: float | None = None,
    protection_eur: float | None = None,
    packaging_eur: float = 0.0,
    home_pickup_eur: float = 0.0,
    bulky_fee_eur: float = 0.0,
    min_roi: float = 0.30,
    min_net_eur: float = 20.0,
) -> dict:
    if buy_price_eur < 0 or sell_price_eur < 0:
        raise ProfitError("buy_price_eur y sell_price_eur no pueden ser negativos.")
    bracket, bracket_note = resolve_bracket(weight_kg, weight_bracket)
    assumptions = [
        bracket_note,
        "Sin comisión de venta para el particular. El comprador paga envío y protección.",
        "En la reventa, el ingreso del vendedor es el precio de venta menos recogida, voluminoso y embalaje.",
    ]
    cost_is_ceiling = False
    if shipping_eur is None:
        shipping = shipping_ceiling(bracket)
        cost_is_ceiling = True
        assumptions.append(
            f"Envío no visto en checkout: techo de referencia {shipping:.2f} EUR para {bracket}. "
            "No es tarifa oficial."
        )
    else:
        shipping = money(shipping_eur)
        assumptions.append(f"Envío de checkout: {shipping:.2f} EUR.")
    if protection_eur is None:
        protection = protection_ceiling(buy_price_eur)
        cost_is_ceiling = True
        assumptions.append(
            f"Protección no vista en checkout: techo de referencia {protection:.2f} EUR "
            "(6 % del precio de compra, entre 1,49 y 25). No es tarifa oficial."
        )
    else:
        protection = money(protection_eur)
        assumptions.append(f"Protección de checkout: {protection:.2f} EUR.")
    if bulky_fee_eur:
        assumptions.append(f"Recargo voluminoso del vendedor: {bulky_fee_eur:.2f} EUR.")
    if home_pickup_eur:
        assumptions.append(f"Recogida a domicilio descontada al vender: {home_pickup_eur:.2f} EUR.")
    if packaging_eur:
        assumptions.append(f"Embalaje: {packaging_eur:.2f} EUR.")

    acquisition = buy_price_eur + shipping + protection
    deductions = packaging_eur + home_pickup_eur + bulky_fee_eur
    proceeds = sell_price_eur - deductions
    net = proceeds - acquisition
    if acquisition > 0:
        roi_raw = net / acquisition
        roi: float | None = round(roi_raw, 4)
        passes_roi = roi_raw > min_roi
    else:
        roi = None
        passes_roi = False
        assumptions.append("Coste de compra 0: el ROI no se calcula. Solo cuenta el neto en euros.")
    passes_net = net > min_net_eur
    reliability = "low" if cost_is_ceiling else "high"
    method = "reference_ceiling_v1" if cost_is_ceiling else "checkout_observed"
    return {
        "weight_bracket": bracket,
        "inbound_shipping_eur": money(shipping),
        "inbound_protection_eur": money(protection),
        "acquisition_eur": money(acquisition),
        "seller_deductions_eur": money(deductions),
        "proceeds_eur": money(proceeds),
        "net_eur": money(net),
        "roi": roi,
        "passes": passes_roi or passes_net,
        "passes_roi": passes_roi,
        "passes_net": passes_net,
        "cost_is_ceiling": cost_is_ceiling,
        "reliability": reliability,
        "method": method,
        "assumptions": assumptions,
    }
