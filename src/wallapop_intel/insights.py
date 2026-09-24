"""Heurísticas documentadas. Toda cifra estimada se etiqueta como tal.

engagement_v1 (fiabilidad alta si hay counters de /items/{id}):
    score = min(100, 8*favoritos + 0.35*visitas + 12*conversaciones)
    Ajustes solo si NO hay counters (fiabilidad baja):
        +1.5 por foto (máx. 8), +6 si bump, +4 si top profile,
        + hasta 10 por antigüedad < 21 días.
    Las visitas/favoritos reales no se inventan: si faltan, views_estimated
    se calcula aparte y se marca method=heuristic_v1, reliability=low.

Oportunidad: precio <= mediana * (1 - 0.22), anuncio no reservado,
score = descuento*100 + recencia. No usa reputación si no se ha pedido el vendedor.

Ganadores: agrupación por las 3 primeras palabras significativas del título.
score = demanda_media (favoritos y visitas de la muestra con ficha) / (1 + log(n))
        * (1 + (p75-p25)/mediana). Requiere al menos 3 anuncios en el grupo.
"""

from __future__ import annotations

import math
import re
from statistics import median
from typing import Any

from wallapop_intel.config import HEURISTIC_VERSION, HEURISTIC_WEIGHTS
from wallapop_intel.models import ItemCard, Opportunity, Winner
from wallapop_intel.normalize import age_days

_STOP = {
    "de", "del", "la", "el", "los", "las", "y", "en", "con", "para", "un", "una",
    "por", "al", "the", "a", "of", "nuevo", "nueva", "usado", "usada",
}


def engagement(card: ItemCard, *, photo_count: int = 0, bumped: bool = False, top_profile: bool = False, created_raw: Any = None) -> tuple[float, str, str, str]:
    w = HEURISTIC_WEIGHTS
    has_real = card.views is not None or card.favorites is not None
    fav = card.favorites or 0
    views = card.views or 0
    conv = card.conversations or 0
    score = w["favorites"] * fav + w["views"] * views + w["conversations"] * conv
    if has_real:
        return round(min(100.0, score), 2), "high", "api_counters", (
            f"Counters reales de GET /items/{{id}}: {views} visitas, {fav} favoritos, {conv} conversaciones. "
            f"score = min(100, {w['favorites']}*fav + {w['views']}*views + {w['conversations']}*conv) = {min(100.0, score):.1f}. "
            f"Método {HEURISTIC_VERSION}. No es una estimación."
        )
    photos = min(photo_count, int(w["photo_cap"])) * w["photo_points"]
    bump = w["bump_points"] if bumped else 0.0
    top = w["top_profile_points"] if top_profile else 0.0
    days = age_days(created_raw)
    recency = 0.0
    if days is not None:
        recency = max(0.0, (w["recency_window_days"] - days) / w["recency_window_days"] * 10.0)
    estimated = photos + bump + top + recency
    return round(min(100.0, estimated), 2), "low", "heuristic_v1", (
        "La ficha no trajo counters. Score ESTIMADO (fiabilidad baja) por fotos, bump, perfil top y antigüedad. "
        "No representa visitas reales. Activa la ficha con wp_get_item o include_details."
    )


def apply_engagement(card: ItemCard, **kwargs: Any) -> ItemCard:
    score, reliability, method, _ = engagement(card, **kwargs)
    return card.model_copy(update={"engagement_score": score, "reliability": reliability, "metrics_method": method})


def prices(cards: list[ItemCard]) -> list[float]:
    return [c.price_eur for c in cards if c.price_eur is not None and c.price_eur > 0]


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * p
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] * (high - rank) + ordered[high] * (rank - low)


def price_stats(cards: list[ItemCard]) -> dict[str, float | None]:
    vals = prices(cards)
    if not vals:
        return {"min": None, "max": None, "median": None, "p25": None, "p75": None}
    return {
        "min": min(vals),
        "max": max(vals),
        "median": float(median(vals)),
        "p25": percentile(vals, 0.25),
        "p75": percentile(vals, 0.75),
    }


def opportunities(cards: list[ItemCard], limit: int = 8) -> tuple[float | None, list[Opportunity]]:
    stats = price_stats(cards)
    med = stats["median"]
    if med is None or med <= 0:
        return None, []
    threshold = med * (1 - HEURISTIC_WEIGHTS["opportunity_discount"])
    found: list[Opportunity] = []
    for card in cards:
        if card.price_eur is None or card.price_eur <= 0 or card.reserved:
            continue
        if card.price_eur > threshold:
            continue
        discount = (med - card.price_eur) / med
        recency_bonus = 5.0 if card.created_at else 0.0
        score = round(discount * 100 + recency_bonus, 2)
        found.append(
            Opportunity(
                item=card,
                score=score,
                median_price=round(med, 2),
                discount_ratio=round(discount, 3),
                why=(
                    f"Precio {card.price_eur:.0f} EUR frente a mediana {med:.0f} EUR "
                    f"({discount:.0%} por debajo; umbral {HEURISTIC_WEIGHTS['opportunity_discount']:.0%}). "
                    "Revisa estado y descripción antes de tratarlo como ganga: la heurística no lee el texto."
                ),
            )
        )
    found.sort(key=lambda row: row.score, reverse=True)
    return round(med, 2), found[:limit]


def _cluster_key(title: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", title.lower())
    kept = [tok for tok in tokens if tok not in _STOP and len(tok) > 1]
    return " ".join(kept[:3]) or title.lower()[:40]


def winners(cards: list[ItemCard], limit: int = 5) -> list[Winner]:
    groups: dict[str, list[ItemCard]] = {}
    for card in cards:
        groups.setdefault(_cluster_key(card.title), []).append(card)
    ranked: list[Winner] = []
    min_n = int(HEURISTIC_WEIGHTS["winner_min_samples"])
    for label, group in groups.items():
        if len(group) < min_n:
            continue
        stats = price_stats(group)
        med = stats["median"] or 0.0
        spread = 0.0
        if med and stats["p75"] is not None and stats["p25"] is not None:
            spread = (stats["p75"] - stats["p25"]) / med
        favs = [c.favorites for c in group if c.favorites is not None]
        views = [c.views for c in group if c.views is not None]
        mean_fav = sum(favs) / len(favs) if favs else 0.0
        mean_views = sum(views) / len(views) if views else 0.0
        demand = mean_fav * 2 + mean_views * 0.05
        if not favs and not views:
            demand = sum((c.engagement_score or 0) for c in group) / len(group)
        score = demand / (1 + math.log(len(group))) * (1 + spread)
        ranked.append(
            Winner(
                label=label,
                sample_size=len(group),
                median_price=round(med, 2) if med else None,
                mean_favorites=round(mean_fav, 2) if favs else None,
                mean_views=round(mean_views, 2) if views else None,
                score=round(score, 2),
                why=(
                    f"Grupo '{label}' con {len(group)} anuncios en la muestra. "
                    f"Demanda media fav={mean_fav:.1f} views={mean_views:.1f}; "
                    f"dispersión de precio {spread:.0%}. "
                    "Si mean_* es null, el score usa solo el engagement estimado (fiabilidad baja)."
                ),
            )
        )
    ranked.sort(key=lambda row: row.score, reverse=True)
    return ranked[:limit]


def price_advice(your_price: float | None, med: float | None, age: float | None, photos: int, description: str | None) -> list[str]:
    tips: list[str] = []
    if your_price is None:
        tips.append("No hay precio legible. Revisa la ficha.")
        return tips
    if med:
        ratio = your_price / med
        if ratio > 1.15:
            tips.append(
                f"Tu precio ({your_price:.0f} EUR) está un {(ratio - 1):.0%} por encima de la mediana de la muestra ({med:.0f} EUR). "
                "Prueba un precio cercano a la mediana si quieres rotar antes."
            )
        elif ratio < 0.8:
            tips.append(
                f"Tu precio ({your_price:.0f} EUR) está por debajo de la mediana ({med:.0f} EUR). "
                "Si el estado es bueno, hay margen para subirlo sin salirte del mercado."
            )
        else:
            tips.append(f"Tu precio ({your_price:.0f} EUR) está alineado con la mediana de la muestra ({med:.0f} EUR).")
    if age is not None and age > 14:
        tips.append(f"El anuncio tiene unos {age:.0f} días. Renovar título o precio suele recuperar visibilidad en anuncios parados.")
    if photos < 3:
        tips.append(f"Solo hay {photos} foto(s). Sube al menos 4 (frontal, detalle, etiqueta, defecto) para competir.")
    if not description or len(description) < 40:
        tips.append("La descripción es corta. Añade estado, capacidad, accesorios y si hay envío.")
    if not tips:
        tips.append("No hay una acción urgente: precio, fotos y texto están en rango razonable respecto a la muestra.")
    return tips
