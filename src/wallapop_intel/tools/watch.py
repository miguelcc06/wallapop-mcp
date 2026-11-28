"""Watchlist local. No publica ni modifica anuncios en Wallapop."""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from wallapop_intel.context import client, store
from wallapop_intel.models import (
    WatchAddInput,
    WatchListInput,
    WatchRemoveInput,
    WatchResponse,
    WatchSnapshotInput,
    WatchTargetInput,
)
from wallapop_intel.tools.common import fail
from wallapop_intel.watchlist import run_snapshot

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
_LIST = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


def register(mcp: FastMCP) -> None:
    @mcp.tool(name="wp_watchlist_add", title="Añadir a vigilancia", annotations=_LOCAL_ADD)
    async def wp_watchlist_add(params: WatchAddInput) -> WatchResponse:
        """Guarda un anuncio o una keyword en la watchlist local (PostgreSQL).

        No llama a Wallapop. Repetir la misma clave actualiza la etiqueta.
        Con target_price activa la alerta de precio: wp_watchlist_snapshot (o el
        CLI programado wallapop-watch) avisa cuando el precio sea <= ese valor.

        Returns:
            WatchResponse con la lista completa.
        """
        try:
            store().watch_add(params.kind, params.key.strip(), params.label, params.target_price)
            entries = store().watch_list()
            target = f" con alerta ≤ {params.target_price:g} EUR" if params.target_price else ""
            return WatchResponse(
                summary=f"Añadido {params.kind}:{params.key}{target}. Vigilados: {len(entries)}.",
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

    @mcp.tool(name="wp_watchlist_set_target", title="Fijar alerta de precio", annotations=_LOCAL_ADD)
    async def wp_watchlist_set_target(params: WatchTargetInput) -> WatchResponse:
        """Fija o quita (sin target_price) el umbral de alerta de una entrada ya vigilada.

        Returns:
            WatchResponse con la lista completa.
        """
        try:
            changed = store().watch_set_target(params.kind, params.key.strip(), params.target_price)
            if not changed:
                raise ToolError(f"{params.kind}:{params.key} no está en la watchlist. Añádela con wp_watchlist_add.")
            entries = store().watch_list()
            what = f"alerta ≤ {params.target_price:g} EUR" if params.target_price else "alerta quitada"
            return WatchResponse(summary=f"{params.kind}:{params.key}: {what}.", entries=entries)
        except Exception as exc:
            fail(exc)

    @mcp.tool(name="wp_watchlist_list", title="Listar vigilancia", annotations=_LIST)
    async def wp_watchlist_list(params: WatchListInput) -> WatchResponse:
        """Devuelve la watchlist local sin llamar a Wallapop ni crear snapshots.

        Returns:
            WatchResponse.entries con kind, key, label, target_price y created_at.
        """
        try:
            entries = store().watch_list()
            lines = [
                f"- {row.get('kind')}:{row.get('key')} ({row.get('label') or 'sin etiqueta'})"
                + (f" · alerta ≤ {row['target_price']:g} EUR" if row.get("target_price") else "")
                for row in entries
            ]
            body = "\n".join(lines) if lines else "Vacía. Añade con wp_watchlist_add."
            return WatchResponse(summary=f"# Watchlist ({len(entries)})\n\n{body}", entries=entries)
        except Exception as exc:
            fail(exc)

    @mcp.tool(name="wp_watchlist_snapshot", title="Snapshot de la vigilancia", annotations=_SNAP)
    async def wp_watchlist_snapshot(params: WatchSnapshotInput) -> WatchResponse:
        """Lee el estado actual de cada entrada vigilada, lo guarda con timestamp y evalúa las alertas.

        keyword: una búsqueda newest (máx. 15). item: una ficha. Máx. 8 entradas por
        pasada; un fallo en una no aborta las demás. alerts lista las entradas con
        target_price cumplido (new=false si ya se avisó por Telegram). El aviso
        automático lo da el CLI wallapop-watch con un timer; esta tool no avisa.

        Returns:
            WatchResponse.entries (resultado de la pasada) y WatchResponse.alerts.
        """
        try:
            if not store().watch_list():
                return WatchResponse(
                    summary="La watchlist está vacía. Usa wp_watchlist_add con kind=item o kind=keyword.",
                    entries=[],
                )
            report, alerts = await run_snapshot(client(), store())
            lines = [
                f"- {'🔔 ' if a['new'] else ''}{a['kind']}:{a['key']} → {a['title']} ({a['item_id']}) "
                f"{a['price']:g} EUR ≤ {a['target_price']:g}"
                for a in alerts
            ]
            alert_txt = "\n\nAlertas de precio:\n" + "\n".join(lines) if lines else ""
            return WatchResponse(
                summary=f"Snapshot de {len(report)} entradas. Consulta wp_price_history para ver la serie.{alert_txt}",
                entries=report,
                alerts=alerts,
            )
        except Exception as exc:
            fail(exc)
