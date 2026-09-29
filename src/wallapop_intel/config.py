"""Configuración por variables de entorno. No hay secretos en código."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    latitude: float
    longitude: float
    user_id: str | None
    proxy: str | None
    rate_limit_rps: float
    min_delay_ms: int
    jitter_ms: int
    max_retries: int
    timeout_s: float
    db_path: Path
    snapshot_ttl_days: int
    sign_requests: bool
    enrich_html: bool
    transport: str
    http_host: str
    http_port: int
    language: str
    base_url: str = "https://api.wallapop.com/api/v3"
    web_origin: str = "https://es.wallapop.com"

    @classmethod
    def from_env(cls) -> Settings:
        rps = max(0.2, min(_env_float("WALLAPOP_RATE_LIMIT_RPS", 1.0), 1.0))
        min_delay = max(500, _env_int("WALLAPOP_MIN_DELAY_MS", 500))
        user = os.environ.get("WALLAPOP_USER_ID", "").strip() or None
        proxy = os.environ.get("WALLAPOP_PROXY", "").strip() or None
        db = Path(os.environ.get("WALLAPOP_DB_PATH", "data/wallapop_intel.sqlite"))
        transport = os.environ.get("WALLAPOP_TRANSPORT", "stdio").strip().lower()
        if transport not in {"stdio", "http", "streamable-http"}:
            transport = "stdio"
        return cls(
            latitude=_env_float("WALLAPOP_LATITUDE", 40.416775),
            longitude=_env_float("WALLAPOP_LONGITUDE", -3.703790),
            user_id=user,
            proxy=proxy,
            rate_limit_rps=rps,
            min_delay_ms=min_delay,
            jitter_ms=max(0, _env_int("WALLAPOP_JITTER_MS", 250)),
            max_retries=max(1, _env_int("WALLAPOP_MAX_RETRIES", 3)),
            timeout_s=_env_float("WALLAPOP_TIMEOUT_S", 20.0),
            db_path=db,
            snapshot_ttl_days=max(1, _env_int("WALLAPOP_SNAPSHOT_TTL_DAYS", 90)),
            sign_requests=_env_bool("WALLAPOP_SIGN_REQUESTS", False),
            enrich_html=_env_bool("WALLAPOP_ENRICH_HTML", False),
            transport=transport,
            http_host=os.environ.get("WALLAPOP_HTTP_HOST", "127.0.0.1"),
            http_port=_env_int("WALLAPOP_HTTP_PORT", 8000),
            language=os.environ.get("WALLAPOP_LANGUAGE", "es-ES"),
        )


# Pesos de la heurística v1. Documentados en RESEARCH.md. El agente los ve en cada respuesta.
HEURISTIC_VERSION = "engagement_v1"
HEURISTIC_WEIGHTS = {
    "favorites": 8.0,
    "views": 0.35,
    "conversations": 12.0,
    "photo_cap": 8,
    "photo_points": 1.5,
    "bump_points": 6.0,
    "top_profile_points": 4.0,
    "recency_window_days": 21.0,
    "opportunity_discount": 0.22,
    "winner_min_samples": 3,
}

# Protección Wallapop aproximada que paga el comprador: fijo + porcentaje del precio del artículo.
# No es la tarifa del checkout (depende del importe y puede tener topes). Sept-2026.
PROTECTION_FIXED_EUR = 2.5
PROTECTION_RATE = 0.05

# Tarifas orientativas de Wallapop Envíos en península, punto de entrega (media del rango
# publicado por tramo). El precio real depende de operador, modalidad y destino.
# Por encima de 30 kg el servicio es Envíos XL y no entra en esta tabla.
SHIPPING_BANDS: tuple[dict[str, float | str], ...] = (
    {"code": "0-2", "label": "0-2 kg", "max_kg": 2.0, "eur": 2.95},
    {"code": "2-5", "label": "2-5 kg", "max_kg": 5.0, "eur": 3.99},
    {"code": "5-10", "label": "5-10 kg", "max_kg": 10.0, "eur": 6.49},
    {"code": "10-20", "label": "10-20 kg", "max_kg": 20.0, "eur": 10.49},
    {"code": "20-30", "label": "20-30 kg", "max_kg": 30.0, "eur": 15.49},
)

# Wallapop Envíos (orientativo, sept-2026). Rango inferior/superior donde aplica.
SHIPPING_TIERS_KG: list[tuple[float, float, float, float]] = [
    # (max_kg_exclusive_upper, min_eur, max_eur, default_eur)
    (2.0, 2.95, 3.95, 3.45),
    (5.0, 4.95, 4.95, 4.95),
    (10.0, 7.95, 7.95, 7.95),
    (20.0, 9.95, 12.95, 11.45),
    (9999.0, 14.95, 19.95, 16.95),
]
BUYER_PROTECTION_BASE_EUR = 2.50
BUYER_PROTECTION_RATE = 0.05
