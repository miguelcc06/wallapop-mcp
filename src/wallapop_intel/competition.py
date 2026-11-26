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
    "placa": r"placa(?: base| madre)?|motherboard|mainboard|board",
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
# «600 G2», «600/800 G2», «705 G4 SFF», «800 SFF G3» (ya sin signos).
_GEN_MODELS = re.compile(
    r"\b((?:\d{3} (?:[ouy] )?)+)(?:(?:sff|mt|dm|mini|tower) )?(g\d{1,2}(?: g\d{1,2})*)\b"
)
_PLAIN_MODEL = re.compile(rf"\b(?:{_PC_LINES}) (\d{{3,4}})\b|\b(z\d{{3}})\b")
# Gamas que comparten fuente y placa dentro de la misma generación.
_SHARED_PARTS = {"600": "600/800", "800": "600/800"}
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


def equipment_models(text: str) -> list[tuple[str, str | None]]:
    """Pares (modelo, generación) de equipos de marca en un texto normalizado."""
    models: list[tuple[str, str | None]] = [
        (number, generation)
        for found in _GEN_MODELS.finditer(text)
        for number in re.findall(r"\d{3}", found.group(1))
        for generation in found.group(2).split()
    ]
    numbers = {number for number, _ in models}
    plain = (found.group(1) or found.group(2) for found in _PLAIN_MODEL.finditer(text))
    models += [(number, None) for number in plain if number not in numbers]
    return models


def _part_refs(title: str) -> set[str]:
    """Referencias de pieza: «795231-001», «L77487-001», «D10-240P1A»."""
    refs = set()
    for token in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", title.lower()):
        compact = token.replace("-", "")
        if re.fullmatch(r"\d{6}-\d{3}", token) or (
            len(compact) >= 7
            and len(re.findall(r"\d", compact)) >= 4
            and re.search(r"[a-z]", compact)
            and not re.match(rf"i[3579]|lga|ddr|ryzen|{_PC_LINES}", compact)
        ):
            refs.add(compact)
    return refs


def _model_query(text: str) -> list[str]:
    """Marca, modelo y generación, sin la gama: «hp 705 g4»."""
    models = equipment_models(text)
    brand = _find(_BRANDS, text)
    if brand:
        name = brand.group(0)
    else:
        name = "hp" if _find("prodesk|elitedesk", text) else None
    if not models or not name:
        return []
    return [name, *(part for part in models[0] if part)]


def reduced_query(title: str) -> str:
    """Consulta corta para la segunda búsqueda: tipo de pieza, marca y modelo."""
    text = _norm(title)
    kind = product_type(title)
    model_query = _model_query(text)
    if model_query and kind in {"pc", "fuente", "placa"}:
        prefix = {"fuente": ["fuente"], "placa": ["placa", "base"]}.get(kind, [])
        return " ".join(prefix + model_query)
    if kind == "pc":
        # Se corta en el procesador o la capacidad: queda marca y modelo.
        cut = _find(rf"intel|amd|{_CPU_FAMILY}|\d+ ?(?:gb|tb)", text)
        if cut and len(text[: cut.start()].split()) >= 2:
            text = text[: cut.start()]
        return " ".join(text.split()[:5])
    if kind == "cpu" and (model := _CPU_MODEL.search(text)):
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


def _brand_matches(own: str, other: str) -> bool:
    brand = _find(_BRANDS, own)
    if not brand:
        return True
    # Las gamas ProDesk y EliteDesk identifican a HP aunque no se nombre.
    accepted = "hp|prodesk|elitedesk" if brand.group(0) == "hp" else brand.group(0)
    return _find(accepted, other) is not None


def _part_keys(text: str) -> set[tuple[str, str | None]]:
    return {
        (_SHARED_PARTS.get(number, number) if generation else number, generation)
        for number, generation in equipment_models(text)
    }


def _same_product(own: str, other: str, kind: str, has_ref: bool, shared_ref: bool) -> bool:
    """Marca y modelo, con la exigencia que pide cada tipo (textos normalizados)."""
    if kind == "cpu":
        model = _CPU_MODEL.search(own)
        if model:  # «6500T» no es «6500»; vale «i5 6500» o «i5-6500».
            return _find(model.group(1), other) is not None
    if kind in {"fuente", "placa"}:
        if shared_ref:
            return True
        # Piezas de equipo de marca: solo modelo y generación compatibles.
        keys = _part_keys(own)
        if keys:
            return bool(keys & _part_keys(other)) and _brand_matches(own, other)
        # Una fuente identificada solo por su referencia exige esa referencia.
        if kind == "fuente" and has_ref:
            return False
    brand = _find(_BRANDS, own)
    if kind == "placa":  # El modelo manda: «Placa base Spitfire» vale sin la marca.
        generic = _STOP | {"placa", "base", "madre", "motherboard"}
        models = {t for t in own.split() if len(t) > 2 and t not in generic and not (brand and t == brand.group(0))}
        if models:
            return bool(models & set(other.split()))
    if not _brand_matches(own, other):
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
        refs = _part_refs(own_title)
        shared_ref = bool(refs & _part_refs(other_title))
        return product_type(other_title) == kind and _same_product(own, other, kind, bool(refs), shared_ref)
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
