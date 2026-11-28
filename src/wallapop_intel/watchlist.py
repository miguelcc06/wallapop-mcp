"""Snapshot de la watchlist con alertas de precio. Compartido por la tool MCP y el CLI programado."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import Any

from wallapop_intel.client import WallapopClient
from wallapop_intel.config import Settings
from wallapop_intel.db import Store
from wallapop_intel.insights import apply_engagement
from wallapop_intel.models import ItemCard
from wallapop_intel.normalize import to_card
from wallapop_intel.tools.common import cards_from_search, snapshot_card

logger = logging.getLogger("wallapop_intel.watchlist")

MAX_ENTRIES = 8
KEYWORD_SAMPLE = 15
MAX_ALERTS_PER_KEYWORD = 5


def price_alert(card: ItemCard, target: float | None) -> bool:
    return target is not None and card.price_eur is not None and card.price_eur <= target


def _alert(entry: dict[str, Any], card: ItemCard, db: Store) -> dict[str, Any]:
    price = float(card.price_eur or 0)
    return {
        "kind": entry["kind"],
        "key": entry["key"],
        "item_id": card.id,
        "title": card.title,
        "price": price,
        "target_price": entry["target_price"],
        "url": card.url,
        "reserved": bool(card.reserved),
        "new": not db.alert_seen(entry["kind"], entry["key"], card.id, price),
    }


async def run_snapshot(api: WallapopClient, db: Store) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Persiste un snapshot por entrada y evalúa los umbrales. Devuelve (informe, alertas).

    Un fallo en una entrada no aborta las demás. No marca las alertas como avisadas.
    """
    report: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []
    for entry in db.watch_list()[:MAX_ENTRIES]:
        target = entry.get("target_price")
        try:
            if entry["kind"] == "item":
                card = apply_engagement(to_card(await api.get_item(entry["key"])))
                snapshot_card(card, None, card.metrics_method, card.reliability)
                report.append(
                    {"kind": "item", "key": entry["key"], "title": card.title, "price": card.price_eur,
                     "views": card.views, "favorites": card.favorites, "target_price": target}
                )
                if price_alert(card, target):
                    alerts.append(_alert(entry, card, db))
            else:
                payload = await api.search(entry["key"], order_by="newest")
                cards, _ = cards_from_search(payload, "any", KEYWORD_SAMPLE)
                for card in cards:
                    snapshot_card(card, entry["key"], "search_listing", "medium")
                report.append({"kind": "keyword", "key": entry["key"], "sampled": len(cards), "target_price": target})
                hits = sorted(
                    (c for c in cards if not c.reserved and price_alert(c, target)),
                    key=lambda c: c.price_eur or 0,
                )
                alerts.extend(_alert(entry, card, db) for card in hits[:MAX_ALERTS_PER_KEYWORD])
        except Exception as exc:
            logger.warning("Snapshot de %s:%s falló: %s", entry["kind"], entry["key"], exc)
            report.append({"kind": entry["kind"], "key": entry["key"], "error": f"{type(exc).__name__}: {exc}"})
    return report, alerts


def format_alert(alert: dict[str, Any]) -> str:
    from wallapop_intel.monitor import escape_markdown, format_price

    lines = [
        "🔔 *Alerta de precio en Wallapop*",
        f"• *Vigilancia:* {escape_markdown(alert['kind'])}:{escape_markdown(alert['key'])}",
        f"• *Título:* {escape_markdown(alert['title'])}",
        f"• *Precio:* {format_price(alert['price'])} € (objetivo {format_price(alert['target_price'])} €)",
    ]
    if alert.get("reserved"):
        lines.append("• *Estado:* reservado")
    lines.append(f"• *Enlace:* {escape_markdown(alert.get('url') or 'sin enlace')}")
    return "\n".join(lines)


def notify_new(alerts: list[dict[str, Any]], db: Store, send: Callable[[str], None]) -> int:
    """Envía las alertas nuevas y las marca solo si el envío tuvo éxito."""
    sent = 0
    for alert in alerts:
        if not alert["new"]:
            continue
        send(format_alert(alert))
        db.alert_mark(alert["kind"], alert["key"], alert["item_id"], alert["price"])
        sent += 1
    return sent


async def _run_pass(*, dry_run: bool) -> int:
    from wallapop_intel.context import settings, store
    from wallapop_intel.monitor import load_telegram_credentials, send_telegram

    cfg: Settings = settings()
    db = store()
    api = WallapopClient(cfg)
    await api.open()
    try:
        report, alerts = await run_snapshot(api, db)
    except BaseException:
        db.close()
        raise
    finally:
        await api.close()
    new = [a for a in alerts if a["new"]]
    logger.info("Entradas: %s · alertas: %s (nuevas %s)", len(report), len(alerts), len(new))
    try:
        if dry_run:
            for alert in new:
                print(format_alert(alert))
        elif new:
            token, chat = load_telegram_credentials()
            notify_new(alerts, db, lambda text: send_telegram(token, chat, text))
    finally:
        db.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wallapop-watch",
        description="Snapshot de la watchlist y aviso por Telegram cuando se cumple un precio objetivo.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Imprime las alertas nuevas sin enviar ni marcarlas")
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        return asyncio.run(_run_pass(dry_run=args.dry_run))
    except Exception:
        logger.exception("El pase de la watchlist falló")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
