"""Filtro de la muestra de competencia de un anuncio propio (sin red)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from statistics import median, quantiles

from wallapop_intel.lots import classify_listing

MIN_SAMPLE = 3

# Los patrones se aplican al título en minúsculas, sin acentos ni signos.
# Sustantivos que nombran una pieza suelta.
_PARTS = {
    "fuente": r"fuente|psu",
    "placa": r"placa(?: base| madre)?|motherboard",
    "lector": r"lector|grabador|grabadora",
    "cpu": r"procesador|microprocesador|cpu",
    "gpu": r"grafica|gpu",
    "ram": r"memoria",
    "disco": r"disco",
    "disipador": r"disipador|ventilador",
}
# Palabras que solo nombran la pieza si abren el título («8GB RAM» describe un PC).
_LEADING_PARTS = {"ram": r"ram", "disco": r"ssd|hdd|nvme", "gpu": r"gtx|rtx|radeon|geforce"}
_PC_WORDS = r"pcs?|ordenador(?:es)?|sobremesa|torre|portatil|equipo|workstation"
_PC_LINES = r"prodesk|elitedesk|compaq|pavilion|optiplex|thinkcentre|esprimo|veriton|ideacentre"
_CPU_FAMILY = r"i[3579]|ryzen|a(?:4|6|8|10|12)|xeon|pentium|celeron"
_CPU_MODEL = re.compile(
    r"\b(?:i[3579] ?|ryzen [3579] |a(?:4|6|8|10|12) |xeon (?:e[357] )?)(\d{4,5}[a-z]{0,2})\b"
)
_BRANDS = r"hp|dell|lenovo|asus|acer|msi|gigabyte|asrock|corsair|seasonic|fujitsu|apple|samsung|kingston|crucial|evga"
_LOT = r"lotes?|conjuntos?"
_BROKEN = r"reparar|piezas|despiece|rot[oa]s?|defectuos[oa]s?|averiad[oa]s?|no enciende|no arranca|no funciona"
# Equipos vendidos sin componentes: no son comparables con uno completo.
_INCOMPLETE = r"sin (?:ram|memoria|ssd|hdd|disco|procesador|cpu)"
_STOP = {"de", "del", "la", "el", "para", "con", "en", "y", "a", "por", "vendo", "nuevo", "nueva", "usado"}


@dataclass(frozen=True)
class Candidate:
    id: str
    title: str
    price: float | None
    user_id: str | None = None


def _norm(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"[a-z0-9]+", plain))


def _find(pattern: str, text: str) -> re.Match[str] | None:
    return re.search(rf"\b(?:{pattern})\b", text)


def product_type(title: str) -> str | None:
    """Tipo de producto: «pc» para un equipo completo o el nombre de la pieza.

    Un título con procesador y capacidad («i5 16GB») es un equipo, no una CPU.
    """
    text = _norm(title)
    parts = [(m.start(), name) for name, p in _PARTS.items() if (m := _find(p, text))]
    parts += [(0, name) for name, p in _LEADING_PARTS.items() if re.match(rf"(?:{p})\b", text)]
    first = min(parts) if parts else None
    pc_word = _find(_PC_WORDS, text)
    if pc_word and (first is None or pc_word.start() < first[0]):
        return "pc"
    has_specs = _find(_CPU_FAMILY, text) and _find(r"\d+ ?(?:gb|tb)|ssd|windows", text)
    if has_specs and (first is None or first[0] > 0 or first[1] == "cpu"):
        return "pc"
    if first:
        return first[1]
    if _find(_PC_LINES, text):
        return "pc"
    return "cpu" if _find(_CPU_FAMILY, text) else None


def key_tokens(title: str) -> list[str]:
    return [t for t in _norm(title).split() if t not in _STOP and len(t) > 1]


def reduced_query(title: str) -> str:
    """Consulta corta para la segunda búsqueda: tipo de pieza, marca y modelo."""
    text = _norm(title)
    if product_type(title) == "pc":
        # Se corta en el procesador o la capacidad: queda marca y modelo.
        cut = _find(rf"intel|amd|{_CPU_FAMILY}|\d+ ?(?:gb|tb)", text)
        if cut and len(text[: cut.start()].split()) >= 2:
            text = text[: cut.start()]
        return " ".join(text.split()[:5])
    if product_type(title) == "cpu" and (model := _CPU_MODEL.search(text)):
        return f"procesador {model.group(0)}"
    # En piezas sobran las referencias numéricas largas y los adjetivos de montaje.
    words = [
        w for w in text.split()
        if w not in _STOP | {"interno", "interna", "sata", "pc"} and not re.fullmatch(r"\d{3,}", w)
    ]
    return " ".join(words[:4])


def _is_lot(text: str) -> bool:
    found = classify_listing(text)
    return bool(_find(_LOT, text)) or bool(found and "pack" in found["kinds"])


def _is_broken(text: str) -> bool:
    found = classify_listing(text)
    return bool(_find(_BROKEN, text)) or bool(found and "teardown" in found["kinds"])


def _same_product(own: str, other: str, kind: str) -> bool:
    """Marca y modelo, con la exigencia que pide cada tipo (textos normalizados)."""
    if kind == "cpu":
        model = _CPU_MODEL.search(own)
        if model:  # «6500T» no es «6500»; vale «i5 6500» o «i5-6500».
            return _find(model.group(1), other) is not None
    brand = _find(_BRANDS, own)
    if kind == "placa":  # El modelo manda: «Placa base Spitfire» vale sin la marca.
        generic = _STOP | {"placa", "base", "madre", "motherboard"}
        models = {t for t in own.split() if len(t) > 2 and t not in generic and not (brand and t == brand.group(0))}
        if models:
            return bool(models & set(other.split()))
    # Las gamas ProDesk y EliteDesk identifican a HP aunque no se nombre.
    accepted = "hp|prodesk|elitedesk" if brand and brand.group(0) == "hp" else brand and brand.group(0)
    if brand and not _find(accepted, other):
        return False
    if kind == "pc":
        line = re.search(rf"\b(?:{_PC_LINES}) (\d{{3,4}})\b", own)
        required = [line.group(1)] if line else []
        generation = _find(r"g\d{1,2}", own)
        if generation:
            required.append(generation.group(0))
        if not all(_find(token, other) for token in required):
            return False
        mine, theirs = _find(r"i[3579]", own), _find(r"i[3579]", other)
        return not (mine and theirs and mine.group(0) != theirs.group(0))
    if kind == "lector" and _find(r"dvd", own):
        return _find(r"dvd(?: ?rw| ?rom)?|cd", other) is not None
    return True


def _comparable(own_title: str, other_title: str) -> bool:
    own, other = _norm(own_title), _norm(other_title)
    if (_is_lot(other) and not _is_lot(own)) or (_is_broken(other) and not _is_broken(own)):
        return False
    if _find(_INCOMPLETE, other) and not _find(_INCOMPLETE, own):
        return False
    kind = product_type(own_title)
    if kind:
        return product_type(other_title) == kind and _same_product(own, other, kind)
    tokens = set(key_tokens(own_title))
    shared = tokens & set(key_tokens(other_title))
    return bool(shared) and len(shared) >= min(2, len(tokens))


def filter_competitors(
    own: Candidate, candidates: list[Candidate], own_user_id: str
) -> list[Candidate]:
    """Quita propios, otros productos, lotes, averiados y outliers (0,25x-4x la mediana)."""
    kept = [
        c
        for c in candidates
        if c.id != own.id
        and c.user_id != own_user_id
        and c.price
        and c.price > 0
        and _comparable(own.title, c.title)
    ]
    if not kept:
        return []
    mid = median(c.price for c in kept)
    return [c for c in kept if 0.25 * mid <= c.price <= 4 * mid]


def summarize(own_price: float | None, sample: list[Candidate]) -> dict:
    """Muestra, mediana, p25-p75, posición del precio y consejo."""
    prices = [c.price for c in sample]
    if len(prices) < MIN_SAMPLE:
        return {
            "count": len(prices), "median": None, "p25": None, "p75": None, "position": None,
            "advice": f"No hay muestra comparable ({len(prices)} de {MIN_SAMPLE} mínimos): no se valora el precio.",
        }
    p25, mid, p75 = quantiles(prices, n=4, method="inclusive")
    if own_price is None:
        position, advice = None, f"Tu anuncio no tiene precio; la mediana es {mid:.2f} EUR."
    elif own_price < p25:
        position, advice = "por debajo", f"Puedes subir el precio hacia {p25:.2f}-{mid:.2f} EUR."
    elif own_price > p75:
        position, advice = "por encima", f"Baja el precio hacia {mid:.2f}-{p75:.2f} EUR."
    else:
        position, advice = "en rango", f"Precio en rango ({p25:.2f}-{p75:.2f} EUR)."
    return {"count": len(prices), "median": mid, "p25": p25, "p75": p75, "position": position, "advice": advice}
