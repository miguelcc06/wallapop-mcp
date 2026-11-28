"""Cliente HTTP de solo lectura para api.wallapop.com/api/v3.

Verificado el 2026-09-24: búsqueda, ficha, usuario, stats y categorías responden
200 sin X-Signature desde este entorno, con ritmo ≤1 req/s. La paginación usa
el JWT de meta.next_page como query `next_page` (start_cursor repite la página).

Verificado el 2026-11-26: /users/{id} solo acepta el id hash (con el numérico
responde 404, y 400 en /items y /stats). /users/{id}/items pagina con el valor
de meta.next como query `since`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlparse

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


_USER_HASH = re.compile(r"(?=[0-9]*[a-z])[0-9a-z]{12}")
_USER_SLUG = re.compile(r"[\w.~-]*-(\d+)")
_ITEM_HASH = re.compile(r"(?=[0-9]*[a-z])[0-9a-z]{8,16}")
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
# Tope de seguridad contra cursores sin fin; si se alcanza, el listado se marca truncado.
_MAX_ITEM_PAGES = 100


class WallapopError(Exception):
    """Error accionable para el agente."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ItemListing:
    """Anuncios de un perfil y, si la lista no está completa, por qué."""

    rows: list[dict[str, Any]]
    truncated: str | None = None
    error: WallapopError | None = None


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

    def defer(self, seconds: float) -> None:
        """No dar turnos nuevos antes de que pasen estos segundos (Retry-After)."""
        self._next = max(self._next, time.monotonic() + seconds)


class WallapopClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._bucket = TokenBucket(
            settings.rate_limit_rps,
            settings.min_delay_ms / 1000.0,
            settings.jitter_ms / 1000.0,
        )
        self._client: httpx.AsyncClient | None = None
        self._user_ids: dict[str, str] = {}
        self._item_ids: dict[str, str] = {}

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
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        url = f"{self.settings.base_url}{path}"
        if clean:
            url = f"{url}?{urlencode(clean, doseq=True)}"
        response = await self._get(url, path)
        try:
            return response.json()
        except ValueError as exc:
            raise WallapopError(
                "La API devolvió 200 pero el cuerpo no es JSON. "
                "Reintenta; si persiste, el endpoint cambió."
            ) from exc

    async def _get(self, url: str, path: str) -> httpx.Response:
        """GET con ritmo y reintentos. Devuelve la respuesta 200 o lanza WallapopError."""
        await self.open()
        assert self._client is not None
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
                return response

            if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < self.settings.max_retries:
                retry_after = response.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else min(12.0, 1.2 * (2**attempt))
                except ValueError:
                    wait = min(12.0, 1.2 * (2**attempt))
                logger.warning("HTTP %s en %s; reintento en %.1fs", response.status_code, path, wait)
                if retry_after:
                    # Se respeta aunque quien llama cancele esta petición por falta de tiempo.
                    self._bucket.defer(wait)
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

    async def get_user_items(self, user_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Anuncios publicados de un perfil (id hash): todos, o los primeros limit."""
        listing = await self.list_user_items(user_id, limit)
        if listing.error:
            raise listing.error
        return listing.rows

    async def list_user_items(
        self, user_id: str, limit: int | None = None, timeout_s: float | None = None
    ) -> ItemListing:
        """Sigue meta.next hasta el final, limit o timeout_s (tiempo total).

        Si falla la primera página lanza WallapopError. Un fallo posterior devuelve
        lo leído, con el motivo en truncated.
        """
        path = f"/users/{user_id}/items"
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        items: dict[str, dict[str, Any]] = {}
        since: str | None = None
        for page in range(_MAX_ITEM_PAGES):
            try:
                left = None if deadline is None else max(0.0, deadline - time.monotonic())
                payload = await asyncio.wait_for(self.request_json(path, {"since": since}), left)
                rows = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(rows, list):
                    raise WallapopError(f"Respuesta inesperada en {path}: falta la lista 'data'.")
            except (TimeoutError, WallapopError) as exc:
                error = exc if isinstance(exc, WallapopError) else WallapopError(
                    f"Sin tiempo para leer {path} (página {page + 1})."
                )
                if not page:
                    raise error from exc
                return ItemListing(list(items.values()), str(error), error)
            before = len(items)
            for row in rows:
                if isinstance(row, dict) and row.get("id"):
                    items.setdefault(str(row["id"]), row)
            meta = payload.get("meta")
            since = meta.get("next") if isinstance(meta, dict) else None
            if limit is not None and (len(items) > limit or (len(items) == limit and since)):
                return ItemListing(list(items.values())[:limit], f"se pidió limit={limit} y hay más anuncios")
            if not since:
                return ItemListing(list(items.values()))
            if len(items) == before:
                return ItemListing(list(items.values()), "el cursor de paginación dejó de avanzar")
        return ItemListing(list(items.values()), f"se alcanzó el tope de {_MAX_ITEM_PAGES} páginas")

    async def resolve_user_id(self, value: str) -> str:
        """Convierte id hash, slug o URL de perfil en el id hash que pide la API v3.

        El id numérico suelto no se puede convertir: la web solo sirve el perfil
        con el slug exacto y la API no ofrece búsqueda por número.
        """
        raw = value.strip()
        if raw in self._user_ids:
            return self._user_ids[raw]
        ref = raw
        if "/" in raw:
            parsed = urlparse(raw if "://" in raw else f"https://{raw}")
            host = parsed.hostname or ""
            parts = [part for part in parsed.path.split("/") if part]
            if not (host == "wallapop.com" or host.endswith(".wallapop.com")) or len(parts) < 2 or parts[0] != "user":
                raise WallapopError(_user_ref_message(raw))
            ref = unquote(parts[1])
        if _USER_HASH.fullmatch(ref):
            return ref
        match = _USER_SLUG.fullmatch(ref)
        if not match or ref.startswith("-"):
            raise WallapopError(_user_ref_message(raw))
        user_id = await self._user_id_from_profile(ref, match.group(1))
        self._user_ids[raw] = user_id
        return user_id

    async def resolve_item_id(self, value: str) -> str:
        """Convierte un id hash o una URL de anuncio (es.wallapop.com/item/<slug>) en el id de la API.

        Con URL lee la página pública y busca el id en __NEXT_DATA__. El slug de la
        URL lleva un número que no es el id de la API, por eso hace falta la página.
        """
        raw = value.strip()
        if "/" not in raw and "." not in raw:
            if _ITEM_HASH.fullmatch(raw):
                return raw
            raise WallapopError(_item_ref_message(raw))
        if raw in self._item_ids:
            return self._item_ids[raw]
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = parsed.hostname or ""
        parts = [part for part in parsed.path.split("/") if part]
        if not (host == "wallapop.com" or host.endswith(".wallapop.com")):
            raise WallapopError(_item_ref_message(raw))
        if "item" not in parts or parts.index("item") + 1 >= len(parts):
            raise WallapopError(_item_ref_message(raw))
        slug = unquote(parts[parts.index("item") + 1])
        path = f"/item/{quote(slug)}"
        try:
            response = await self._get(f"{self.settings.web_origin}{path}", path)
        except WallapopError as exc:
            if exc.status_code != 404:
                raise
            raise WallapopError(
                f"No existe {self.settings.web_origin}{path}: el anuncio se vendió o se retiró, o la URL está incompleta.",
                404,
            ) from exc
        found = _NEXT_DATA.search(response.text)
        try:
            data = json.loads(found.group(1)) if found else None
        except ValueError:
            data = None
        item_id = _find_item_id(data, slug)
        if item_id is None:
            raise WallapopError(
                f"No se pudo leer el id del anuncio en {path}: la página cambió. "
                "Pasa el id hash (12 caracteres) en su lugar."
            )
        self._item_ids[raw] = item_id
        return item_id

    async def _user_id_from_profile(self, slug: str, number: str) -> str:
        path = f"/user/{quote(slug)}"
        try:
            response = await self._get(f"{self.settings.web_origin}{path}", path)
        except WallapopError as exc:
            if exc.status_code != 404:
                raise
            raise WallapopError(
                f"No existe el perfil {self.settings.web_origin}{path}. El slug tiene que ser exacto: "
                "copia la URL completa del perfil o usa el id hash.",
                404,
            ) from exc
        found = _NEXT_DATA.search(response.text)
        try:
            user = json.loads(found.group(1))["props"]["pageProps"]["user"] if found else None
            user_id = str(user["id"])
            web_slug = str(user["webSlug"])
        except (ValueError, KeyError, TypeError) as exc:
            raise WallapopError(
                f"No se pudo leer el id de usuario en {path}: la página cambió. "
                "Pon el id hash directamente (campo user.id de la ficha de un anuncio tuyo)."
            ) from exc
        if not _USER_HASH.fullmatch(user_id) or not web_slug.endswith(f"-{number}"):
            raise WallapopError(f"La página {path} no corresponde al perfil pedido. Usa el id hash.")
        return user_id

    async def get_user_reviews(self, user_id: str) -> list[dict[str, Any]]:
        data = await self.request_json(f"/users/{user_id}/reviews")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        return []


def _find_item_id(node: Any, slug: str) -> str | None:
    """Busca en el JSON de la página un objeto cuyo slug sea el de la URL y que tenga un id hash."""
    fallback: str | None = None
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            ident = cur.get("id")
            if isinstance(ident, str) and _ITEM_HASH.fullmatch(ident):
                if slug in (cur.get("webSlug"), cur.get("web_slug"), cur.get("slug")):
                    return ident
            stack.extend(cur.values())
            if fallback is None:
                item = cur.get("item")
                if isinstance(item, dict) and isinstance(item.get("id"), str) and _ITEM_HASH.fullmatch(item["id"]):
                    fallback = item["id"]
        elif isinstance(cur, list):
            stack.extend(cur)
    return fallback


def _item_ref_message(value: str) -> str:
    return (
        f"'{value}' no es un anuncio reconocible. Usa el id hash (p. ej. 'w67v2xx3896x') "
        "o la URL completa https://es.wallapop.com/item/<slug>."
    )


def _user_ref_message(value: str) -> str:
    reason = (
        "es el id numérico antiguo, que la API v3 no acepta ni permite convertir"
        if value.isdigit()
        else "no es un id de usuario reconocible"
    )
    return (
        f"El usuario '{value}' {reason}. Pon en WALLAPOP_USER_ID el id hash (12 caracteres, "
        "campo user.id de la ficha de un anuncio tuyo, visible con wp_get_item como vendedor) "
        "o la URL completa de tu perfil: https://es.wallapop.com/user/<nombre>-<número>."
    )


def _http_message(response: httpx.Response) -> str:
    url = response.request.url
    snippet = response.text[:280].replace("\n", " ").strip() or "(cuerpo vacío)"
    code = response.status_code
    where = f"HTTP {code} en GET {url.path}"
    if code == 403:
        return (
            f"{where}: CloudFront o el edge rechazó la petición (IP no residencial o bot-score). "
            "Activa WALLAPOP_PROXY con una IP residencial o espera y reduce la frecuencia. "
            f"Respuesta: {snippet}"
        )
    if code == 429:
        return (
            f"{where}: rate limit. Espera el Retry-After y no superes 1 petición/segundo. "
            f"Respuesta: {snippet}"
        )
    if code == 404:
        return f"{where}: recurso no encontrado. Puede haberse vendido o el id es incorrecto. Respuesta: {snippet}"
    if code == 400:
        params = ", ".join(url.params.keys()) or "ninguno"
        hint = ""
        if "order_by" in url.params:
            hint = (
                "Revisa order_by, category_id y el cursor. "
                "order_by 'relevance' no existe; usa 'most_relevance'. "
            )
        elif url.path.startswith("/api/v3/users/"):
            hint = "Comprueba el id de usuario: la API v3 usa el id hash, no el numérico. "
        return f"{where}: petición rechazada (parámetros: {params}). {hint}Respuesta: {snippet}"
    if code >= 500:
        return (
            f"{where}: fallo temporal de Wallapop. Reintenta más tarde; no subas la frecuencia. "
            f"Respuesta: {snippet}"
        )
    return f"{where}. Respuesta: {snippet}"


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
