"""Normalización de anuncios y fechas a partir del JSON real de la API v3."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from wallapop_intel.models import ItemCard


def price_amount(raw: dict[str, Any]) -> tuple[float | None, str | None]:
    price = raw.get("price")
    if isinstance(price, dict):
        if "amount" in price and isinstance(price["amount"], (int, float)):
            return float(price["amount"]), price.get("currency") or "EUR"
        cash = price.get("cash")
        if isinstance(cash, dict) and isinstance(cash.get("amount"), (int, float)):
            return float(cash["amount"]), cash.get("currency") or "EUR"
    return None, None


def epoch_to_iso(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    seconds = value / 1000.0 if value > 10_000_000_000 else float(value)
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return None


def age_days(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    seconds = value / 1000.0 if value > 10_000_000_000 else float(value)
    delta = datetime.now(timezone.utc).timestamp() - seconds
    return max(0.0, delta / 86400.0)


def counters(raw: dict[str, Any]) -> dict[str, int | None]:
    block = raw.get("counters")
    if not isinstance(block, dict):
        return {"views": None, "favorites": None, "conversations": None}
    out: dict[str, int | None] = {}
    for key in ("views", "favorites", "conversations"):
        val = block.get(key)
        out[key] = int(val) if isinstance(val, (int, float)) else None
    return out


def image_url(raw: dict[str, Any]) -> str | None:
    images = raw.get("images") or []
    if not images or not isinstance(images, list):
        return None
    first = images[0]
    if not isinstance(first, dict):
        return None
    urls = first.get("urls") or first.get("urls_by_size") or {}
    if isinstance(urls, dict):
        return urls.get("medium") or urls.get("small") or urls.get("big")
    return None


def city_of(raw: dict[str, Any]) -> str | None:
    loc = raw.get("location")
    if isinstance(loc, dict):
        return loc.get("city")
    return None


def slug_of(raw: dict[str, Any]) -> str | None:
    return raw.get("web_slug") or raw.get("slug")


def item_url(raw: dict[str, Any]) -> str | None:
    share = raw.get("share_url")
    if isinstance(share, str) and share:
        return share
    slug = slug_of(raw)
    if slug:
        return f"https://es.wallapop.com/item/{slug}"
    return None


def title_of(raw: dict[str, Any]) -> str:
    title = raw.get("title")
    if isinstance(title, dict):
        return str(title.get("original") or title.get("translated") or "")
    return str(title or "")


def description_of(raw: dict[str, Any]) -> str | None:
    desc = raw.get("description")
    if isinstance(desc, dict):
        text = desc.get("original") or desc.get("translated")
        return str(text) if text else None
    if isinstance(desc, str):
        return desc
    return None


def created_raw(raw: dict[str, Any]) -> Any:
    return raw.get("created_at") or raw.get("creation_date") or raw.get("modified_date") or raw.get("modified_at")


def to_card(raw: dict[str, Any]) -> ItemCard:
    amount, currency = price_amount(raw)
    ctr = counters(raw)
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    user_id = raw.get("user_id") or user.get("id")
    cat = raw.get("category_id")
    if cat is None and isinstance(raw.get("taxonomy"), list) and raw["taxonomy"]:
        cat = raw["taxonomy"][0].get("id")
    shipping = raw.get("shipping") if isinstance(raw.get("shipping"), dict) else {}
    reserved = raw.get("reserved")
    if isinstance(reserved, dict):
        reserved = reserved.get("flag")
    return ItemCard(
        id=str(raw.get("id")),
        title=title_of(raw),
        price_eur=amount,
        currency=currency,
        city=city_of(raw),
        user_id=str(user_id) if user_id else None,
        category_id=str(cat) if cat is not None else None,
        web_slug=slug_of(raw),
        url=item_url(raw),
        created_at=epoch_to_iso(created_raw(raw)),
        reserved=bool(reserved) if isinstance(reserved, bool) else None,
        shippable=bool(shipping.get("item_is_shippable")) if "item_is_shippable" in shipping else None,
        image_url=image_url(raw),
        views=ctr["views"],
        favorites=ctr["favorites"],
        conversations=ctr["conversations"],
    )


def within_timeframe(raw: dict[str, Any], timeframe: str) -> bool:
    if timeframe in {"", "any", None}:
        return True
    days = age_days(raw.get("created_at") or raw.get("creation_date"))
    if days is None:
        return True
    if timeframe == "today":
        return days <= 1.0
    if timeframe == "lastWeek":
        return days <= 7.0
    if timeframe == "lastMonth":
        return days <= 31.0
    return True
