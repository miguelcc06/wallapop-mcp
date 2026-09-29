"""Clasificación local de lotes, packs, despieces y urgencias.

No llama a Wallapop. La tool wp_search_lots busca y luego aplica esto
sobre título y descripción de la muestra.
"""

from __future__ import annotations

import re

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("teardown", re.compile(r"\b(despieces?|por\s+piezas|para\s+piezas|para\s+reparar|no\s+funciona|averiad[oa]s?)\b", re.I)),
    ("lot", re.compile(r"\b(lotes?|conjuntos?|torres?|equipos?\s+completos?|ordenadores?\s+completos?|pcs?\s+completos?)\b", re.I)),
    ("pack", re.compile(r"\b(packs?|kits?)\b", re.I)),
    ("urgent", re.compile(r"\b(urgentes?|mudanzas?|liquidaci[oó]n|remato|hoy\s+mismo)\b", re.I)),
)

_SUFFIX = {
    "lot": "lote",
    "pack": "pack",
    "teardown": "despiece",
    "urgent": "urgente",
    "any": "lote",
}

_DOMINANT = ("teardown", "lot", "pack", "urgent")


def search_keywords(keywords: str, focus: str) -> str:
    base = " ".join(keywords.split())
    suffix = _SUFFIX.get(focus)
    if not suffix:
        raise ValueError(f"focus '{focus}' no es válido. Usa lot, pack, teardown, urgent o any.")
    if re.search(rf"\b{re.escape(suffix)}\b", base, flags=re.I):
        return base[:120]
    return f"{base} {suffix}"[:120]


def classify_listing(title: str, description: str | None = None) -> dict | None:
    text = f"{title or ''}\n{description or ''}"
    found: dict[str, list[str]] = {}
    for kind, pattern in _PATTERNS:
        hits = [match.group(0).lower() for match in pattern.finditer(text)]
        if hits:
            found[kind] = hits
    if not found:
        return None
    kinds = [kind for kind in _DOMINANT if kind in found]
    signals: list[str] = []
    for kind in kinds:
        for token in found[kind]:
            if token not in signals:
                signals.append(token)
    dominant = kinds[0]
    kind = dominant if len(kinds) == 1 else "mixed"
    return {"kind": kind, "kinds": kinds, "signals": signals}
