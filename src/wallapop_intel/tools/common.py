"""Helpers de tools: errores, resumen markdown y snapshots."""

from __future__ import annotations

from typing import Any

from fastmcp.exceptions import ToolError

from wallapop_intel.client import WallapopError, extract_cursor, extract_items
from wallapop_intel.context import store
from wallapop_intel.db import utcnow
from wallapop_intel.insights import apply_engagement
from wallapop_intel.models import ItemCard, ResponseFormat
from wallapop_intel.normalize import to_card, within_timeframe


def fail(exc: Exception) -> None:
    if isinstance(exc, ToolError):
        raise exc
    if isinstance(exc, WallapopError):
        raise ToolError(str(exc)) from exc
    raise ToolError(
        f"Error inesperado ({type(exc).__name__}): {exc}. "
        "Si es de red, reintenta una vez; si es 403, configura WALLAPOP_PROXY."
    ) from exc


def missing_user() -> None:
    raise ToolError(
        "Falta WALLAPOP_USER_ID. Copia tu id público de Wallapop (está en la URL del perfil, "
        "es.wallapop.com/user/...) a la variable de entorno y reinicia el servidor. "
        "Esta tool solo lee el perfil público; no inicia sesión."
    )


def render(fmt: ResponseFormat, summary: str, payload: Any) -> Any:
    if fmt == ResponseFormat.json:
        return payload
    return payload.model_copy(update={"summary": summary}) if hasattr(payload, "model_copy") else payload


def snapshot_card(card: ItemCard, keyword: str | None, method: str | None, reliability: str | None) -> None:
    store().add_snapshot(
        {
            "item_id": card.id,
            "captured_at": utcnow(),
            "keyword": (keyword or "").lower() or None,
            "title": card.title,
            "price_amount": card.price_eur,
            "currency": card.currency,
            "available": 0 if card.reserved else 1,
            "reserved": 1 if card.reserved else 0,
            "views": card.views,
            "favorites": card.favorites,
            "conversations": card.conversations,
            "metrics_method": method,
            "reliability": reliability,
            "seller_id": card.user_id,
            "category_id": card.category_id,
        }
    )


def cards_from_search(payload: dict[str, Any], timeframe: str, limit: int) -> tuple[list[ItemCard], str | None]:
    raw_items = extract_items(payload)
    cards: list[ItemCard] = []
    for raw in raw_items:
        if not within_timeframe(raw, timeframe):
            continue
        card = apply_engagement(to_card(raw))
        cards.append(card)
        if len(cards) >= limit:
            break
    return cards, extract_cursor(payload)


def bullets(cards: list[ItemCard]) -> str:
    lines = []
    for card in cards:
        price = f"{card.price_eur:.0f} {card.currency or 'EUR'}" if card.price_eur is not None else "s/p"
        extra = ""
        if card.views is not None or card.favorites is not None:
            extra = f" · {card.views or 0} visitas · {card.favorites or 0} fav"
        lines.append(f"- {card.title} ({card.id}) — {price} — {card.city or 'ubicación n/d'}{extra}")
    return "\n".join(lines) or "_Sin anuncios._"
