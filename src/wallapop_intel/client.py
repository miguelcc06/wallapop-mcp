"""Cliente HTTP de solo lectura para api.wallapop.com/api/v3.

Verificado el 2026-09-24: búsqueda, ficha, usuario, stats y categorías responden
200 sin X-Signature desde este entorno, con ritmo ≤1 req/s. La paginación usa
el JWT de meta.next_page como query `next_page` (start_cursor repite la página).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import random
import time
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from wallapop_intel.config import Settings

logger = logging.getLogger("wallapop_intel.client")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
]

# Clave pública histórica del cliente web (aviso de empleo, no un secreto de cuenta).
# Solo se usa si WALLAPOP_SIGN_REQUESTS=1. En la verificación de sept-2026 no hacía falta.
_LEGACY_SIGN_KEY = (
    "Tm93IHRoYXQgeW91J3ZlIGZvdW5kIHRoaXMsIGFyZSB5b3UgcmVhZHkgdG8gam9pbiB1cz8gam9ic0B3YWxsYXBvcC5jb20=="
)

ORDER_BY = {
    "newest",
    "price_low_to_high",
    "price_high_to_low",
    "most_relevance",
    "closest",
}


class WallapopError(Exception):
    """Error accionable para el agente."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def legacy_x_signature(method: str, url_path_and_query: str, timestamp_ms: str) -> str:
    """HMAC-SHA256 estilo cliente web: METHOD|path?query|timestamp|."""
    payload = f"{method.upper()}|{url_path_and_query}|{timestamp_ms}|"
    digest = hmac.new(_LEGACY_SIGN_KEY.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


class TokenBucket:
    def __init__(self, rps: float, min_delay_s: float, jitter_s: float) -> None:
        self.min_interval = max(1.0 / rps, min_delay_s)
        self.jitter_s = jitter_s
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            jitter = random.uniform(0, self.jitter_s) if self.jitter_s else 0.0
            self._next = max(now, self._next) + self.min_interval + jitter
        if delay:
            await asyncio.sleep(delay)


class WallapopClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._bucket = TokenBucket(
            settings.rate_limit_rps,
            settings.min_delay_ms / 1000.0,
            settings.jitter_ms / 1000.0,
        )
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> WallapopClient:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def open(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.settings.timeout_s,
                proxy=self.settings.proxy,
                follow_redirects=False,
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self, method: str, url: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": self.settings.language,
            "Origin": self.settings.web_origin,
            "Referer": self.settings.web_origin + "/",
            "User-Agent": random.choice(USER_AGENTS),
            "X-DeviceOS": "0",
        }
        if self.settings.sign_requests:
            parsed = urlparse(url)
            path_q = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            ts = str(int(time.time() * 1000))
            headers["Timestamp"] = ts
            headers["X-Signature"] = legacy_x_signature(method, path_q, ts)
        return headers

    async def request_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await self.open()
        assert self._client is not None
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        url = f"{self.settings.base_url}{path}"
        if clean:
            url = f"{url}?{urlencode(clean, doseq=True)}"
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries):
            await self._bucket.wait()
            try:
                response = await self._client.get(url, headers=self._headers("GET", url))
            except httpx.TimeoutException as exc:
                last_error = exc
                await asyncio.sleep(min(8.0, 0.8 * (2**attempt)))
                continue
            except httpx.RequestError as exc:
                last_error = exc
                await asyncio.sleep(min(8.0, 0.8 * (2**attempt)))
                continue

            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise WallapopError(
                        "La API devolvió 200 pero el cuerpo no es JSON. "
                        "Reintenta; si persiste, el endpoint cambió."
                    ) from exc

            if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < self.settings.max_retries:
                retry_after = response.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else min(12.0, 1.2 * (2**attempt))
                except ValueError:
                    wait = min(12.0, 1.2 * (2**attempt))
                logger.warning("HTTP %s en %s; reintento en %.1fs", response.status_code, path, wait)
                await asyncio.sleep(wait)
                last_error = WallapopError(_http_message(response), response.status_code)
                continue

            raise WallapopError(_http_message(response), response.status_code)

        raise WallapopError(
            f"Agotados {self.settings.max_retries} reintentos contra {path}. "
            f"Último error: {last_error}. Baja la frecuencia o activa WALLAPOP_PROXY."
        )

    async def search(
        self,
        keywords: str,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        category_id: int | None = None,
        order_by: str = "newest",
        distance_km: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if order_by not in ORDER_BY:
            raise WallapopError(
                f"order_by '{order_by}' no está soportado. "
                f"Usa uno de: {', '.join(sorted(ORDER_BY))}."
            )
        params: dict[str, Any] = {
            "keywords": keywords,
            "latitude": latitude if latitude is not None else self.settings.latitude,
            "longitude": longitude if longitude is not None else self.settings.longitude,
            "source": "search_box",
            "order_by": order_by,
            "min_sale_price": min_price,
            "max_sale_price": max_price,
            "category_id": category_id,
            "distance_in_km": distance_km,
            "next_page": cursor,
        }
        data = await self.request_json("/search", params)
        return data if isinstance(data, dict) else {"data": data}

    async def get_item(self, item_id: str) -> dict[str, Any]:
        data = await self.request_json(f"/items/{item_id}")
        if not isinstance(data, dict) or "id" not in data:
            raise WallapopError(
                f"La ficha {item_id} no tiene la forma esperada. "
                "Comprueba el id con wp_search_items."
            )
        return data

    async def get_categories(self) -> dict[str, Any]:
        data = await self.request_json("/categories")
        if not isinstance(data, dict):
            raise WallapopError("El árbol de categorías no llegó como objeto JSON.")
        return data

    async def get_user(self, user_id: str) -> dict[str, Any]:
        data = await self.request_json(f"/users/{user_id}")
        if not isinstance(data, dict):
            raise WallapopError(f"Perfil {user_id} ilegible. Revisa el user_id.")
        return data

    async def get_user_stats(self, user_id: str) -> dict[str, Any]:
        data = await self.request_json(f"/users/{user_id}/stats")
        return data if isinstance(data, dict) else {}

    async def get_user_items(self, user_id: str) -> list[dict[str, Any]]:
        data = await self.request_json(f"/users/{user_id}/items")
        if isinstance(data, dict):
            rows = data.get("data") or []
            return [row for row in rows if isinstance(row, dict)]
        return []

    async def get_user_reviews(self, user_id: str) -> list[dict[str, Any]]:
        data = await self.request_json(f"/users/{user_id}/reviews")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        return []


def _http_message(response: httpx.Response) -> str:
    snippet = response.text[:280].replace("\n", " ")
    code = response.status_code
    if code == 403:
        return (
            "HTTP 403: CloudFront o el edge rechazó la petición (IP no residencial o bot-score). "
            "Activa WALLAPOP_PROXY con una IP residencial o espera y reduce la frecuencia. "
            f"Respuesta: {snippet}"
        )
    if code == 429:
        return (
            "HTTP 429: rate limit. Espera el Retry-After y no superes 1 petición/segundo. "
            f"Respuesta: {snippet}"
        )
    if code == 404:
        return (
            "HTTP 404: recurso no encontrado. El anuncio puede haberse vendido o el id es incorrecto. "
            f"Respuesta: {snippet}"
        )
    if code == 400:
        return (
            "HTTP 400: parámetros o cabeceras rechazados. Revisa order_by, category_id y el cursor. "
            "order_by 'relevance' no existe; usa 'most_relevance'. "
            f"Respuesta: {snippet}"
        )
    if code >= 500:
        return (
            f"HTTP {code}: fallo temporal de Wallapop. Reintenta más tarde; no subas la frecuencia. "
            f"Respuesta: {snippet}"
        )
    return f"HTTP {code} de Wallapop. Respuesta: {snippet}"


def extract_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") or {}
    section = data.get("section") or {}
    items = (section.get("payload") or {}).get("items")
    if not isinstance(items, list):
        items = section.get("items") or []
    return [item for item in items if isinstance(item, dict)]


def extract_cursor(payload: dict[str, Any]) -> str | None:
    meta = payload.get("meta") or {}
    cursor = meta.get("next_page")
    return cursor if isinstance(cursor, str) and cursor else None
