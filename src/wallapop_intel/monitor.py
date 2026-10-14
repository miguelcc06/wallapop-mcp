"""Monitor determinista de PCs compactos / SFF en Wallapop por debajo de 50 €.

Sin LLM: busca, exige envío disponible, filtra piezas y accesorios por
palabras clave, deduplica en SQLite y avisa por Telegram. Pensado para un
pase (`--once`) o un bucle (`--loop`) y para el timer systemd de cada 10 minutos.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from wallapop_intel.client import WallapopClient, extract_items
from wallapop_intel.config import Settings
from wallapop_intel.normalize import (
    city_of,
    description_of,
    is_shippable,
    item_url,
    price_amount,
    title_of,
)

logger = logging.getLogger("wallapop_intel.monitor")

MAX_PRICE = 50.0
DEFAULT_DB_PATH = Path("data/sff_monitor.db")
DEFAULT_OPENCLAW_PATH = Path("/home/miguelcc06/.openclaw/openclaw.json")
DEFAULT_CHAT_ID = "8548578097"
DEFAULT_INTERVAL_MINUTES = 10.0

SFF_QUERIES: tuple[str, ...] = (
    "hp prodesk",
    "hp elitedesk",
    "optiplex sff",
    "thinkcentre sff",
    "hp 600 g3",
    "hp 600 g2",
    "hp 400 g4",
    "prodesk sff",
    "pc sff",
)

# Piezas, periféricos y accesorios. Coincidencia sin distinguir mayúsculas ni tildes.
EXCLUDE_KEYWORDS: tuple[str, ...] = (
    "fuente",
    "power supply",
    "cable",
    "cargador",
    "adaptador",
    "soporte",
    "funda",
    "teclado",
    "raton",
    "mouse",
    "antena",
    "lector dvd",
    "disipador",
    "ventilador",
    "fan",
    "placa base",
    "motherboard",
    "pantalla",
    "monitor",
    "lote de procesadores",
    "solo procesador",
    "solo cpu",
    "modulo ram",
    "memoria ram",
    "embellecedor",
    "tapa",
    "despiece",
)

# Un despiece que sigue siendo un barebone operativo (chasis + placa + CPU) no se descarta
# aunque mencione estas palabras. El resto de la lista sigue excluyendo.
OVERRIDABLE_KEYWORDS = frozenset({"despiece", "placa base", "motherboard"})

_CHASSIS = ("barebone", "chasis", "chassis")
_BOARD = ("placa base", "motherboard", "placa")
_CPU = ("cpu", "procesador", "i3", "i5", "i7", "i9", "pentium", "celeron")
_WORKING = ("funcionando", "funciona", "working", "operativo", "operativa", "enciende", "arranca")


class TelegramConfigError(RuntimeError):
    """Faltan token o chat de Telegram."""


@dataclass(frozen=True)
class Listing:
    item_id: str
    title: str
    price: float | None
    shippable: bool | None
    city: str | None
    url: str | None
    description: str | None = None


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text or "")
    stripped = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    return stripped.lower()


def contains_keyword(folded: str, keyword: str) -> bool:
    """Palabra o frase, con plural sencillo (s/es) y límite de token."""
    parts = _fold(keyword).split()
    if not parts:
        return False
    body = r"\s+".join(re.escape(part) + r"(?:es|s)?" for part in parts)
    return re.search(rf"(?<![a-z0-9]){body}(?![a-z0-9])", folded) is not None


def matched_exclude_keywords(title: str, description: str | None = None) -> list[str]:
    folded = _fold(f"{title or ''}\n{description or ''}")
    return [keyword for keyword in EXCLUDE_KEYWORDS if contains_keyword(folded, keyword)]


def is_working_barebone(title: str, description: str | None = None) -> bool:
    """Chasis o barebone que incluye placa y CPU y está operativo."""
    folded = _fold(f"{title or ''}\n{description or ''}")
    if not any(contains_keyword(folded, token) for token in _CHASSIS):
        return False
    if not any(contains_keyword(folded, token) for token in _BOARD):
        return False
    if not any(contains_keyword(folded, token) for token in _CPU):
        return False
    return _has_working_signal(folded)


def _has_working_signal(folded: str) -> bool:
    for token in _WORKING:
        for match in re.finditer(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", folded):
            prefix = folded[max(0, match.start() - 3) : match.start()]
            if prefix.endswith("no "):
                continue
            return True
    return False


# Un PC completo casi siempre cita RAM, cable o fuente en la ficha. Esas palabras
# descartan el anuncio si van en el título (el artículo es la pieza) o si el texto
# dice que se vende solo eso, que está averiado o que es para piezas.
_PC_FAMILY = (
    "prodesk",
    "elitedesk",
    "optiplex",
    "thinkcentre",
    "thinkcenter",
    "ordenador",
    "sobremesa",
    "barebone",
    "mini pc",
    "minipc",
)
_DEAD_PHRASES = (
    "para piezas",
    "para reparar",
    "no funciona",
    "no enciende",
    "no arranca",
    "averiado",
    "averiada",
    "estropeado",
    "estropeada",
)
_PART_ONLY_PHRASES = (
    "vendo la fuente",
    "vendo el cable",
    "vendo la memoria",
    "vendo el modulo",
    "vendo el cargador",
    "vendo la placa",
    "vendo el ventilador",
    "vendo el disipador",
    "vendo la tapa",
    "vendo el teclado",
    "vendo el raton",
    "vendo la pantalla",
    "vendo el monitor",
    "solo la fuente",
    "solo el cable",
    "solo la memoria",
    "solo el cargador",
    "solo la placa",
    "solo el ventilador",
    "el pc no esta",
    "el ordenador no esta",
    "sin el pc",
    "sin el ordenador",
    "no incluye el pc",
    "no incluye el ordenador",
    "no incluye la torre",
)


def _blob(title: str, description: str | None) -> str:
    return _fold(f"{title or ''}\n{description or ''}")


def _is_parts_donor_or_dead(folded: str) -> bool:
    if any(phrase in folded for phrase in _PART_ONLY_PHRASES):
        return True
    return any(phrase in folded for phrase in _DEAD_PHRASES)


def _looks_like_whole_pc(folded: str) -> bool:
    if _is_parts_donor_or_dead(folded):
        return False
    return any(token in folded for token in _PC_FAMILY)


def is_spare_or_accessory(title: str, description: str | None = None) -> bool:
    folded = _blob(title, description)
    if _is_parts_donor_or_dead(folded):
        return True
    title_hits = matched_exclude_keywords(title, None)
    if title_hits:
        if set(title_hits) <= OVERRIDABLE_KEYWORDS and is_working_barebone(title, description):
            return False
        return True
    description_hits = matched_exclude_keywords("", description)
    if not description_hits:
        return False
    if _looks_like_whole_pc(folded):
        return False
    if set(description_hits) <= OVERRIDABLE_KEYWORDS and is_working_barebone(title, description):
        return False
    return True


def price_in_range(price: float | None, max_price: float = MAX_PRICE) -> bool:
    if price is None:
        return False
    try:
        amount = float(price)
    except (TypeError, ValueError):
        return False
    return 0 < amount <= max_price


def is_sff_candidate(
    title: str,
    description: str | None = None,
    price: float | None = None,
    *,
    max_price: float = MAX_PRICE,
    shippable: bool | None = None,
) -> bool:
    """True si el anuncio es un PC compacto con envío, dentro del precio y no una pieza.

    Solo entra un anuncio con envío confirmado (``shippable is True``).
    ``False`` (solo en mano) y ``None`` (envío desconocido) no son candidatos.
    """
    if shippable is not True:
        return False
    if not price_in_range(price, max_price):
        return False
    return not is_spare_or_accessory(title, description)


def _needs_description(title: str) -> bool:
    hits = matched_exclude_keywords(title, None)
    if not hits:
        return True
    return set(hits) <= OVERRIDABLE_KEYWORDS


def escape_markdown(text: str) -> str:
    return re.sub(r"([_*`\[])", r"\\\1", text or "")


def format_price(price: float) -> str:
    text = f"{float(price):.2f}"
    if text.endswith("00"):
        return text[:-3]
    if text.endswith("0"):
        return text[:-1]
    return text


def format_telegram_message(listing: Listing) -> str:
    shipping = "📦 Con envío" if listing.shippable else "📍 Solo en mano"
    city = (listing.city or "").strip() or "Sin ubicación"
    url = (listing.url or "").strip() or "sin enlace"
    title = (listing.title or "").strip() or "Sin título"
    price = format_price(listing.price if listing.price is not None else 0)
    lines = [
        "🖥️ *Nuevo PC SFF en Wallapop (< 50€)*",
        f"• *Título:* {escape_markdown(title)}",
        f"• *Precio:* {price} €",
        f"• *Envío:* {shipping}",
        f"• *Ubicación:* {escape_markdown(city)}",
        f"• *Enlace:* {escape_markdown(url)}",
    ]
    return "\n".join(lines)


def listing_from_raw(raw: dict[str, Any]) -> Listing | None:
    item_id = str(raw.get("id") or "").strip()
    if not item_id:
        return None
    amount, _currency = price_amount(raw)
    return Listing(
        item_id=item_id,
        title=title_of(raw),
        price=amount,
        shippable=is_shippable(raw),
        city=city_of(raw),
        url=item_url(raw),
        description=description_of(raw),
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class SeenStore:
    """Deduplicación local. Si el item_id ya está, no se vuelve a avisar."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_items (
                item_id TEXT PRIMARY KEY,
                title TEXT,
                price REAL,
                shippable INTEGER,
                city TEXT,
                url TEXT,
                first_seen_at TIMESTAMP,
                notified_at TIMESTAMP
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SeenStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def seen(self, item_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM seen_items WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        return row is not None

    def remember(self, listing: Listing, *, notified_at: str | None) -> None:
        shippable = None if listing.shippable is None else int(bool(listing.shippable))
        self._conn.execute(
            """
            INSERT OR IGNORE INTO seen_items (
                item_id, title, price, shippable, city, url, first_seen_at, notified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing.item_id,
                listing.title,
                listing.price,
                shippable,
                listing.city,
                listing.url,
                utc_now(),
                notified_at,
            ),
        )
        self._conn.commit()


def resolve_db_path(cli_path: str | None = None, environ: Mapping[str, str] | None = None) -> Path:
    if cli_path:
        return Path(cli_path)
    env = environ if environ is not None else os.environ
    raw = (env.get("SFF_MONITOR_DB") or "").strip()
    if raw:
        return Path(raw)
    return DEFAULT_DB_PATH


def _account_token(account: Mapping[str, Any]) -> str | None:
    for key in ("botToken", "bot_token", "token"):
        value = account.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _account_chat(account: Mapping[str, Any]) -> str | None:
    for key in ("chatId", "chat_id", "defaultChatId", "default_chat_id"):
        value = account.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _find_accounts(node: Any) -> Mapping[str, Any] | None:
    if not isinstance(node, dict):
        return None
    accounts = node.get("accounts")
    if isinstance(accounts, dict) and ("wallapop" in accounts or "oliver_tree" in accounts):
        return accounts
    for value in node.values():
        found = _find_accounts(value)
        if found is not None:
            return found
    return None


def read_openclaw_telegram(path: Path) -> tuple[str | None, str | None]:
    if not path.is_file():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("No se pudo leer %s: %s", path, exc)
        return None, None
    accounts = _find_accounts(data)
    if not accounts:
        return None, None
    for name in ("wallapop", "oliver_tree"):
        account = accounts.get(name)
        if not isinstance(account, dict):
            continue
        token = _account_token(account)
        if token:
            return token, _account_chat(account)
    return None, None


def load_telegram_credentials(
    environ: Mapping[str, str] | None = None,
    openclaw_path: Path | None = None,
) -> tuple[str, str]:
    env = environ if environ is not None else os.environ
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (env.get("TELEGRAM_CHAT_ID") or "").strip()
    file_token, file_chat = read_openclaw_telegram(openclaw_path or DEFAULT_OPENCLAW_PATH)
    if not token:
        token = file_token or ""
    if not chat:
        chat = file_chat or DEFAULT_CHAT_ID
    if not token:
        raise TelegramConfigError(
            "Falta TELEGRAM_BOT_TOKEN. Defínelo en el entorno o en "
            "accounts.wallapop / accounts.oliver_tree de openclaw.json."
        )
    return token, chat


def send_telegram(token: str, chat_id: str, text: str, *, timeout: float = 20.0) -> None:
    response = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=timeout,
    )
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code >= 400 or (isinstance(body, dict) and body.get("ok") is False):
        detail = ""
        if isinstance(body, dict):
            detail = str(body.get("description") or "")
        if not detail:
            detail = response.text[:240]
        raise TelegramConfigError(f"Telegram rechazó el mensaje ({response.status_code}): {detail}")


def _title_blocked(title: str) -> bool:
    hits = matched_exclude_keywords(title, None)
    return bool(hits) and not set(hits) <= OVERRIDABLE_KEYWORDS


async def _description_for(client: WallapopClient, listing: Listing) -> str | None:
    if listing.description:
        return listing.description
    if _title_blocked(listing.title):
        return None
    if not _needs_description(listing.title):
        return None
    try:
        detail = await client.get_item(listing.item_id)
    except Exception as exc:
        logger.warning("Sin ficha de %s (%s); se filtra solo con el título", listing.item_id, exc)
        return None
    return description_of(detail) if isinstance(detail, dict) else None


async def collect_matches(
    client: WallapopClient,
    *,
    queries: Sequence[str] = SFF_QUERIES,
    max_price: float = MAX_PRICE,
) -> list[Listing]:
    found: list[Listing] = []
    seen_ids: set[str] = set()
    errors = 0
    for query in queries:
        try:
            payload = await client.search(query, order_by="newest", max_price=max_price)
        except Exception:
            logger.exception("Búsqueda fallida para %r", query)
            errors += 1
            continue
        items = extract_items(payload if isinstance(payload, dict) else {})
        for raw in items:
            listing = listing_from_raw(raw)
            if listing is None or listing.item_id in seen_ids:
                continue
            seen_ids.add(listing.item_id)
            # Sin envío confirmado no es candidato: no se pide la ficha ni se avisa.
            if listing.shippable is not True:
                continue
            if not price_in_range(listing.price, max_price):
                continue
            description = await _description_for(client, listing)
            if description is not None:
                listing = replace(listing, description=description)
            if not is_sff_candidate(
                listing.title,
                listing.description,
                listing.price,
                max_price=max_price,
                shippable=listing.shippable,
            ):
                continue
            found.append(listing)
    if queries and errors == len(queries):
        raise RuntimeError(f"Todas las búsquedas fallaron ({errors})")
    return found


async def scan_once(
    client: WallapopClient,
    *,
    store: SeenStore | None,
    notify: Callable[[str], None] | None,
    dry_run: bool = False,
    queries: Sequence[str] = SFF_QUERIES,
    max_price: float = MAX_PRICE,
) -> list[Listing]:
    matches = await collect_matches(client, queries=queries, max_price=max_price)
    fresh: list[Listing] = []
    for listing in matches:
        if store is not None and not dry_run and store.seen(listing.item_id):
            continue
        fresh.append(listing)

    if dry_run:
        for listing in fresh:
            print(format_telegram_message(listing))
        logger.info("Dry-run: %s coincidencias (sin Telegram ni SQLite)", len(fresh))
        return fresh

    if store is None:
        raise RuntimeError("Hace falta una base SQLite salvo en --dry-run")

    sent: list[Listing] = []
    for listing in fresh:
        if store.seen(listing.item_id):
            continue
        text = format_telegram_message(listing)
        if notify is None:
            raise RuntimeError("Hace falta una función de aviso salvo en --dry-run")
        notify(text)
        store.remember(listing, notified_at=utc_now())
        sent.append(listing)
    logger.info("Avisos nuevos: %s", len(sent))
    return sent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wallapop-monitor",
        description="Busca PCs compactos / SFF en Wallapop por debajo de 50 € y avisa por Telegram.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Un solo pase de búsqueda (por defecto)")
    mode.add_argument("--loop", action="store_true", help="Repetir el pase cada N minutos")
    parser.add_argument(
        "--interval-minutes",
        type=float,
        default=DEFAULT_INTERVAL_MINUTES,
        help="Minutos entre pases con --loop (defecto: 10)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Imprime coincidencias sin enviar Telegram ni escribir la base",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Ruta SQLite (defecto: data/sff_monitor.db o SFF_MONITOR_DB)",
    )
    return parser


async def _run_pass(db_path: Path, *, dry_run: bool) -> int:
    settings = Settings.from_env()
    client = WallapopClient(settings)
    await client.open()
    try:
        if dry_run:
            await scan_once(client, store=None, notify=None, dry_run=True)
            return 0
        creds: dict[str, str] = {}

        def notify(text: str) -> None:
            if not creds:
                token, chat_id = load_telegram_credentials()
                creds["token"] = token
                creds["chat_id"] = chat_id
            send_telegram(creds["token"], creds["chat_id"], text)

        with SeenStore(db_path) as store:
            await scan_once(client, store=store, notify=notify, dry_run=False)
        return 0
    finally:
        await client.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.interval_minutes <= 0:
        parser.error("--interval-minutes debe ser mayor que 0")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    db_path = resolve_db_path(args.db_path)

    def pass_once() -> int:
        try:
            return asyncio.run(_run_pass(db_path, dry_run=args.dry_run))
        except TelegramConfigError as exc:
            logger.error("%s", exc)
            return 1
        except KeyboardInterrupt:
            raise
        except Exception:
            logger.exception("El pase del monitor falló")
            return 1

    if not args.loop:
        return pass_once()

    while True:
        code = pass_once()
        if code != 0:
            logger.error("Pase con errores (código %s); se reintenta en el siguiente ciclo", code)
        logger.info("Siguiente pase en %s min", args.interval_minutes)
        try:
            time.sleep(args.interval_minutes * 60)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
