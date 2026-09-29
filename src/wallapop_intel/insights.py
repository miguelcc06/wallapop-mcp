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

from wallapop_intel.config import (
    BUYER_PROTECTION_BASE_EUR,
    BUYER_PROTECTION_RATE,
    HEURISTIC_VERSION,
    HEURISTIC_WEIGHTS,
    SHIPPING_TIERS_KG,
)
from wallapop_intel.models import ItemCard, LotOpportunity, Opportunity, Winner
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


_LOT_SIGNALS: list[tuple[str, re.Pattern, float]] = [
    ("lote", re.compile(r"\blotes?\b", re.I), 12.0),
    ("pack", re.compile(r"\bpacks?\b", re.I), 10.0),
    ("conjunto", re.compile(r"\bconjunto\b", re.I), 8.0),
    ("para piezas", re.compile(r"para\s+piezas|solo\s+piezas|piezas\s+sueltas", re.I), 14.0),
    ("repuestos", re.compile(r"\brepuestos?\b", re.I), 10.0),
    ("averiado", re.compile(r"\baveriad[oa]s?\b|\broto?s?\b|\bestropead[oa]s?\b", re.I), 11.0),
    ("no enciende", re.compile(r"no\s+enciende|no\s+funciona|no\s+arranca", re.I), 13.0),
    ("para reparar", re.compile(r"para\s+reparar|reparar\s+o\s+piezas", re.I), 9.0),
    ("urge", re.compile(r"\burge\b|\burgente\b|\bmudanza\b|\bliquidaci[oó]n\b", re.I), 15.0),
    ("varios", re.compile(r"\bvarios\b|\bm[uú]ltiples\b|\bx\d+\b", re.I), 6.0),
]


def lot_signals(text: str) -> list[tuple[str, float]]:
    if not text:
        return []
    hits: list[tuple[str, float]] = []
    for label, pattern, weight in _LOT_SIGNALS:
        if pattern.search(text):
            hits.append((label, weight))
    return hits


def signal_score_from_text(title: str, description: str | None = None) -> tuple[float, list[str]]:
    blob = f"{title} {description or ''}".strip()
    hits = lot_signals(blob)
    if not hits:
        return 0.0, []
    raw = sum(w for _, w in hits)
    score = min(100.0, raw)
    labels = [label for label, _ in hits]
    return round(score, 2), labels


def urgency_score(title: str, description: str | None = None) -> float:
    blob = f"{title} {description or ''}".lower()
    score = 0.0
    if re.search(r"\burge\b|\burgente\b", blob):
        score += 40.0
    if re.search(r"mudanza|liquidaci", blob):
        score += 25.0
    if re.search(r"precio\s+negociable|bajo\s+precio|rebaj", blob):
        score += 10.0
    return min(100.0, score)


def lot_opportunities(
    cards: list[ItemCard],
    *,
    limit: int = 10,
    min_score: float = 15.0,
    descriptions: dict[str, str] | None = None,
) -> tuple[float | None, list[LotOpportunity]]:
    """Detecta lotes/packs/averiados y puntúa oportunidad compuesta."""
    stats = price_stats(cards)
    med = stats["median"]
    desc_map = descriptions or {}
    found: list[LotOpportunity] = []
    for card in cards:
        if card.reserved:
            continue
        desc = desc_map.get(card.id)
        sig_score, labels = signal_score_from_text(card.title, desc)
        if sig_score <= 0:
            continue
        price_score = 0.0
        discount_ratio: float | None = None
        if med and med > 0 and card.price_eur is not None and card.price_eur > 0:
            if card.price_eur <= med:
                discount_ratio = (med - card.price_eur) / med
                price_score = min(100.0, discount_ratio * 120.0)
            else:
                over = (card.price_eur - med) / med
                price_score = max(0.0, 20.0 - over * 40.0)
        urg = urgency_score(card.title, desc)
        composite = round(sig_score * 0.45 + price_score * 0.35 + urg * 0.20, 2)
        if composite < min_score:
            continue
        why_parts = [f"Señales: {', '.join(labels)} (signal {sig_score})."]
        if med and card.price_eur:
            why_parts.append(f"Precio {card.price_eur:.0f} EUR vs mediana muestra {med:.0f} EUR.")
        if urg >= 25:
            why_parts.append(f"Urgencia estimada {urg:.0f}/100.")
        found.append(
            LotOpportunity(
                item=card,
                opportunity_score=composite,
                signal_score=sig_score,
                price_score=round(price_score, 2),
                urgency_score=round(urg, 2),
                signals=labels,
                median_price=round(med, 2) if med else None,
                discount_ratio=round(discount_ratio, 3) if discount_ratio is not None else None,
                why=" ".join(why_parts),
            )
        )
    found.sort(key=lambda row: row.opportunity_score, reverse=True)
    return (round(med, 2) if med else None), found[:limit]


def weight_kg_from_band(band: str) -> float:
    mapping = {
        "under_2kg": 1.5,
        "kg_2_5": 3.5,
        "kg_5_10": 7.5,
        "kg_10_20": 15.0,
        "over_20kg": 25.0,
    }
    return mapping.get(band, 1.5)


def shipping_cost_eur(weight_kg: float, *, conservative: bool = True) -> tuple[float, str]:
    w = max(0.01, weight_kg)
    for max_kg, low, high, default in SHIPPING_TIERS_KG:
        if w <= max_kg:
            cost = high if conservative else low
            if low == high:
                detail = f"tramo ≤{max_kg} kg tarifa fija {cost:.2f} EUR"
            else:
                detail = f"tramo ≤{max_kg} kg rango {low:.2f}-{high:.2f} EUR"
            return round(cost, 2), detail
    last = SHIPPING_TIERS_KG[-1]
    cost = last[2] if conservative else last[1]
    return round(cost, 2), f"tramo >20 kg rango orientativo {last[1]:.2f}-{last[2]:.2f} EUR"


def buyer_protection_fee(sale_price: float) -> float:
    return round(BUYER_PROTECTION_BASE_EUR + BUYER_PROTECTION_RATE * max(0.0, sale_price), 2)


def estimate_resell_profit(
    *,
    purchase_price: float,
    expected_resale_price: float,
    weight_kg: float,
    include_outbound_shipping: bool = True,
    include_inbound_shipping: bool = False,
    include_buyer_protection: bool = True,
    other_costs: float = 0.0,
    conservative_shipping: bool = True,
    weight_band: str | None = None,
) -> dict[str, Any]:
    outbound = 0.0
    inbound = 0.0
    ship_notes: list[str] = []
    if include_outbound_shipping:
        outbound, note = shipping_cost_eur(weight_kg, conservative=conservative_shipping)
        ship_notes.append(f"Envío venta: {note}")
    if include_inbound_shipping:
        inbound, note = shipping_cost_eur(weight_kg, conservative=conservative_shipping)
        ship_notes.append(f"Envío compra: {note}")
    protection = buyer_protection_fee(expected_resale_price) if include_buyer_protection else 0.0
    gross = expected_resale_price - purchase_price
    total_costs = outbound + inbound + protection + other_costs
    net = round(gross - total_costs, 2)
    roi: float | None = None
    if purchase_price > 0:
        roi = round((net / purchase_price) * 100.0, 2)
    assumptions = [
        "Cifras orientativas; tarifas reales de Wallapop Envíos pueden variar por tamaño y campañas.",
        *ship_notes,
    ]
    if include_buyer_protection:
        assumptions.append(
            f"Protección comprador estimada: {BUYER_PROTECTION_BASE_EUR:.2f} EUR + "
            f"{BUYER_PROTECTION_RATE:.0%} del precio de venta = {protection:.2f} EUR "
            "(suele pagarla el comprador; se resta aquí como escenario conservador)."
        )
    else:
        assumptions.append("Protección comprador no restada.")
    return {
        "gross_margin": round(gross, 2),
        "net_profit": net,
        "roi_percent": roi,
        "outbound_shipping_eur": outbound,
        "inbound_shipping_eur": inbound,
        "buyer_protection_eur": protection,
        "other_costs": round(other_costs, 2),
        "total_costs": round(total_costs, 2),
        "weight_kg_used": round(weight_kg, 2),
        "weight_band": weight_band,
        "assumptions": assumptions,
    }
