"""Tiempo hasta desaparecer de la muestra, agregado desde los snapshots locales.

No es tiempo de venta confirmado: un anuncio sale de la muestra por venta, por
retirada, o porque anuncios nuevos lo empujan fuera de la ventana. Se mide desde
el primer snapshot propio (no desde la fecha de publicación) y es una cota inferior.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Any

from wallapop_intel.db import parse_ts

RUN_GAP = timedelta(minutes=10)
_SECONDS_PER_DAY = 86400.0


def group_runs(times: list[datetime], gap: timedelta = RUN_GAP) -> list[list[datetime]]:
    """Agrupa instantes en pasadas: un hueco mayor que `gap` abre una pasada nueva."""
    runs: list[list[datetime]] = []
    for moment in sorted(set(times)):
        if runs and moment - runs[-1][-1] <= gap:
            runs[-1].append(moment)
        else:
            runs.append([moment])
    return runs


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * p
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def kaplan_meier_median(samples: list[tuple[float, bool]]) -> float | None:
    """Mediana de supervivencia. samples = (duración, desapareció). None si la curva no baja de 0,5."""
    if not samples:
        return None
    at_risk = len(samples)
    survival = 1.0
    for duration in sorted({d for d, _ in samples}):
        events = sum(1 for d, e in samples if d == duration and e)
        leaving = sum(1 for d, _ in samples if d == duration)
        if events:
            survival *= 1.0 - events / at_risk
            if survival <= 0.5:
                return duration
        at_risk -= leaving
    return None


def _confidence(gone: int) -> str:
    if gone >= 20:
        return "medium"
    if gone >= 5:
        return "low"
    return "very_low"


def compute_sell_time(
    rows: list[dict[str, Any]],
    *,
    min_absent_runs: int = 2,
    run_gap: timedelta = RUN_GAP,
) -> dict[str, Any]:
    """rows: snapshots de una keyword con item_id, captured_at, price_amount, reserved."""
    parsed = [(row, parse_ts(row["captured_at"])) for row in rows]
    runs = group_runs([moment for _, moment in parsed], run_gap)
    run_index: dict[datetime, int] = {m: i for i, run in enumerate(runs) for m in run}
    base: dict[str, Any] = {
        "runs": len(runs),
        "span_days": 0.0,
        "items_tracked": 0,
        "gone": 0,
        "active": 0,
        "single_sighting": 0,
        "median_days_gone": None,
        "p25_days_gone": None,
        "p75_days_gone": None,
        "km_median_days": None,
        "gone_reserved_share": None,
        "confidence": "very_low",
        "items": [],
    }
    if not runs:
        return base
    base["span_days"] = round((runs[-1][-1] - runs[0][0]).total_seconds() / _SECONDS_PER_DAY, 2)

    seen: dict[str, dict[str, Any]] = {}
    for row, moment in parsed:
        info = seen.setdefault(
            row["item_id"],
            {"first": moment, "last": moment, "last_run": run_index[moment], "title": row.get("title"),
             "price": row.get("price_amount"), "reserved": bool(row.get("reserved"))},
        )
        if moment >= info["last"]:
            info.update(last=moment, last_run=run_index[moment], title=row.get("title") or info["title"],
                        price=row.get("price_amount"), reserved=bool(row.get("reserved")))
        info["first"] = min(info["first"], moment)

    last_run = len(runs) - 1
    samples: list[tuple[float, bool]] = []
    gone_days: list[float] = []
    gone_reserved = 0
    items: list[dict[str, Any]] = []
    for item_id, info in seen.items():
        days = (info["last"] - info["first"]).total_seconds() / _SECONDS_PER_DAY
        gone = last_run - info["last_run"] >= min_absent_runs
        if days <= 0:
            base["single_sighting"] += 1
            continue
        samples.append((days, gone))
        if gone:
            gone_days.append(days)
            gone_reserved += 1 if info["reserved"] else 0
        items.append(
            {"item_id": item_id, "title": info["title"], "last_price": info["price"], "days_in_sample": round(days, 2),
             "status": "gone" if gone else "active", "reserved_at_last_sight": info["reserved"]}
        )
    base["items_tracked"] = len(seen)
    base["gone"] = len(gone_days)
    base["active"] = len(samples) - len(gone_days)
    if gone_days:
        base["median_days_gone"] = round(statistics.median(gone_days), 2)
        base["p25_days_gone"] = round(percentile(gone_days, 0.25) or 0.0, 2)
        base["p75_days_gone"] = round(percentile(gone_days, 0.75) or 0.0, 2)
        base["gone_reserved_share"] = round(gone_reserved / len(gone_days), 2)
    km = kaplan_meier_median(samples)
    base["km_median_days"] = round(km, 2) if km is not None else None
    base["confidence"] = _confidence(len(gone_days))
    items.sort(key=lambda r: (r["status"] != "gone", -r["days_in_sample"]))
    base["items"] = items
    return base
