"""Ficha de un anuncio a partir de su URL, con los datos que piden las demás tools."""

from __future__ import annotations

import re

from fastmcp import FastMCP

from wallapop_intel.context import client
from wallapop_intel.insights import apply_engagement, estimate_resell_profit, weight_kg_from_band
from wallapop_intel.lots import classify_listing
from wallapop_intel.models import ItemFromUrlInput, ItemFromUrlResponse
from wallapop_intel.normalize import description_of, location_of, taxonomy_names, title_of, to_card
from wallapop_intel.tools.common import fail, snapshot_card

_READ = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}
_NOISE = re.compile(r"[^\w\s+.-]", re.UNICODE)


def suggest_keywords(title: str, max_words: int = 6) -> str:
    """Keyword de búsqueda a partir del título: sin signos, primeras palabras."""
    words = _NOISE.sub(" ", title).split()
    return " ".join(words[:max_words])[:120] or title[:120]


def register(mcp: FastMCP) -> None:
    @mcp.tool(name="wp_item_from_url", title="Anuncio desde su URL", annotations=_READ)
    async def wp_item_from_url(params: ItemFromUrlInput) -> ItemFromUrlResponse:
        """Dado el enlace de un anuncio, devuelve su ficha y lo necesario para el resto de tools.

        Resuelve la URL al id de la API (lee la página pública del anuncio), pide la
        ficha con counters reales, guarda un snapshot y clasifica lote/averiado.
        next_calls trae los parámetros ya rellenados para wp_get_seller,
        wp_market_analysis, wp_estimate_profit, wp_watchlist_add, wp_price_history
        y wp_flip_scan. Con resale_price_eur añade además el margen de reventa
        comprando a precio de anuncio.

        Returns:
            ItemFromUrlResponse. Solo lectura en Wallapop.
        """
        try:
            api = client()
            item_id = await api.resolve_item_id(params.url)
            raw = await api.get_item(item_id)
            card = apply_engagement(to_card(raw))
            snapshot_card(card, None, card.metrics_method, card.reliability)
            title = title_of(raw)
            desc = description_of(raw)
            lot = classify_listing(title, desc)
            loc = location_of(raw)
            chars = raw.get("characteristics")
            char_text = chars.get("text") if isinstance(chars, dict) else None
            attrs = raw.get("type_attributes")
            cond = attrs.get("condition") if isinstance(attrs, dict) else None
            condition = cond.get("text") if isinstance(cond, dict) else char_text
            keywords = suggest_keywords(title)

            market: dict = {"keywords": keywords}
            if card.category_id and str(card.category_id).isdigit():
                market["category_id"] = int(card.category_id)
            if "latitude" in loc and "longitude" in loc:
                market["latitude"], market["longitude"] = float(loc["latitude"]), float(loc["longitude"])
            next_calls: dict[str, dict] = {
                "wp_market_analysis": market,
                "wp_watchlist_add": {"kind": "item", "key": item_id, "label": title[:160]},
                "wp_price_history": {"item_id": item_id},
                "wp_item_metrics": {"item_id": item_id},
            }
            if card.user_id:
                next_calls["wp_get_seller"] = {"user_id": card.user_id}
                next_calls["wp_get_seller_items"] = {"user_id": card.user_id}
            profit = None
            if card.price_eur is not None:
                profit_args = {"purchase_price": card.price_eur, "weight_band": params.weight_band.value,
                               "include_inbound_shipping": bool(card.shippable)}
                next_calls["wp_estimate_profit"] = profit_args
                if params.resale_price_eur:
                    profit = estimate_resell_profit(
                        purchase_price=card.price_eur,
                        expected_resale_price=params.resale_price_eur,
                        weight_kg=weight_kg_from_band(params.weight_band.value),
                        include_inbound_shipping=bool(card.shippable),
                        weight_band=params.weight_band.value,
                    )
                    next_calls["wp_estimate_profit"] = {**profit_args, "expected_resale_price": params.resale_price_eur}
                    next_calls["wp_flip_scan"] = {"keywords": keywords, "resale_price_eur": params.resale_price_eur,
                                                  "weight_band": params.weight_band.value}
            roi = profit["roi_percent"] if profit else None
            lines = [
                f"# {card.title} ({item_id})",
                f"- Precio: {card.price_eur if card.price_eur is not None else 'n/d'} {card.currency or 'EUR'}"
                f" · Envío: {'sí' if card.shippable else 'no' if card.shippable is False else 'n/d'}"
                f" · Reservado: {'sí' if card.reserved else 'no'}",
                f"- Ciudad: {card.city or 'n/d'} · Categoría: {' > '.join(taxonomy_names(raw)) or card.category_id or 'n/d'}",
                f"- Visitas: {card.views if card.views is not None else 'n/d'} · "
                f"Favoritos: {card.favorites if card.favorites is not None else 'n/d'} · "
                f"Conversaciones: {card.conversations if card.conversations is not None else 'n/d'}",
                f"- Vendedor: {card.user_id or 'n/d'} · URL: {card.url or params.url}",
                f"- Lote/averiado: {lot['kind'] + ' (' + ', '.join(lot['signals']) + ')' if lot else 'sin señales'}",
            ]
            if profit:
                lines.append(
                    f"- Margen comprando a {card.price_eur:.2f} y vendiendo a {params.resale_price_eur:.2f}: "
                    f"neto {profit['net_profit']:.2f} EUR · ROI {'n/d' if roi is None else f'{roi:.1f}%'}"
                )
            return ItemFromUrlResponse(
                summary="\n".join(lines),
                item_id=item_id,
                item=card,
                description=desc,
                condition=condition,
                characteristics=char_text,
                categories=taxonomy_names(raw),
                location=loc,
                lot_kind=lot["kind"] if lot else None,
                lot_signals=lot["signals"] if lot else [],
                counters_are_real=card.views is not None or card.favorites is not None,
                profit=profit,
                next_calls=next_calls,
                note=(
                    "next_calls son parámetros sugeridos: ajusta peso, precio de reventa y keywords antes de decidir. "
                    "El envío de compra y la protección son estimaciones, no el checkout. "
                    "El id se lee de la página pública; si Wallapop cambia su HTML, pasa el id hash."
                ),
            )
        except Exception as exc:
            fail(exc)
