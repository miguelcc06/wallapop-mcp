"""Watchlist local. No publica ni modifica anuncios en Wallapop."""

from __future__ import annotations

from fastmcp import FastMCP

from wallapop_intel.context import client, store
from wallapop_intel.insights import apply_engagement
from wallapop_intel.models import WatchAddInput, WatchRemoveInput, WatchResponse, WatchSnapshotInput
from wallapop_intel.normalize import to_card
from wallapop_intel.tools.common import fail, snapshot_card

_LOCAL_ADD = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_LOCAL_DEL = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
    "openWorldHint": False,
}
_SNAP = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
}


def register(mcp: FastMCP) -> None:
    @mcp.tool(name="wp_watchlist_add", title="Añadir a vigilancia", annotations=_LOCAL_ADD)
    async def wp_watchlist_add(params: WatchAddInput) -> WatchResponse:
        """Guarda un anuncio o una keyword en la watchlist local (SQLite).

        No llama a Wallapop. Repetir la misma clave actualiza la etiqueta.

        Returns:
            WatchResponse con la lista completa.
        """
        try:
            store().watch_add(params.kind, params.key.strip(), params.label)
            entries = store().watch_list()
            return WatchResponse(
                summary=f"Añadido {params.kind}:{params.key}. Vigilados: {len(entries)}.",
                entries=entries,
            )
        except Exception as exc:
            fail(exc)

    @mcp.tool(name="wp_watchlist_remove", title="Quitar de vigilancia", annotations=_LOCAL_DEL)
    async def wp_watchlist_remove(params: WatchRemoveInput) -> WatchResponse:
        """Borra una entrada de la watchlist local. No toca Wallapop ni los snapshots ya guardados.

        Returns:
            WatchResponse.
        """
        try:
            removed = store().watch_remove(params.kind, params.key.strip())
            entries = store().watch_list()
            return WatchResponse(
                summary=f"Eliminadas {removed} entradas para {params.kind}:{params.key}.",
                entries=entries,
            )
        except Exception as exc:
            fail(exc)

    @mcp.tool(name="wp_watchlist_snapshot", title="Snapshot de la vigilancia", annotations=_SNAP)
    async def wp_watchlist_snapshot(params: WatchSnapshotInput) -> WatchResponse:
        """Lee el estado actual de cada entrada vigilada y lo guarda con timestamp.

        keyword: una búsqueda newest (máx. 15). item: una ficha. El ritmo sigue
        el límite global. La frecuencia la decide el agente; el servidor solo persiste.

        Returns:
            WatchResponse.entries incluye el resultado de esta pasada.
        """
        try:
            entries = store().watch_list()
            if not entries:
                return WatchResponse(
                    summary="La watchlist está vacía. Usa wp_watchlist_add con kind=item o kind=keyword.",
                    entries=[],
                )
            report = []
            for entry in entries[:8]:
                if entry["kind"] == "item":
                    raw = await client().get_item(entry["key"])
                    card = apply_engagement(to_card(raw))
                    snapshot_card(card, None, card.metrics_method, card.reliability)
                    report.append({"kind": "item", "key": entry["key"], "title": card.title, "price": card.price_eur, "views": card.views, "favorites": card.favorites})
                else:
                    payload = await client().search(entry["key"], order_by="newest")
                    from wallapop_intel.tools.common import cards_from_search

                    cards, _ = cards_from_search(payload, "any", 15)
                    for card in cards:
                        snapshot_card(card, entry["key"], "search_listing", "medium")
                    report.append({"kind": "keyword", "key": entry["key"], "sampled": len(cards)})
            return WatchResponse(
                summary=f"Snapshot de {len(report)} entradas. Consulta wp_price_history para ver la serie.",
                entries=report,
            )
        except Exception as exc:
            fail(exc)
