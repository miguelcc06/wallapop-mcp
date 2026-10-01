"""Servidor FastMCP para inteligencia de mercado de Wallapop."""

from __future__ import annotations

from fastmcp import FastMCP

from wallapop_intel.tools import register_all

mcp = FastMCP(
    "wallapop_intel",
    instructions=(
        "Inteligencia de mercado de Wallapop en solo lectura. "
        "Las visitas y favoritos de la ficha (wp_get_item / wp_item_metrics) son reales. "
        "El listado de búsqueda no los trae. El histórico de precios solo existe desde el primer snapshot local. "
        "wp_search_lots detecta lotes/averiados en la muestra; wp_estimate_profit calcula margen neto de reventa. "
        "wp_watchlist_list consulta la base PostgreSQL local. "
        "No hay tools de compra, publicación ni mensajes."
    ),
)

register_all(mcp)


def main() -> None:
    """Punto de entrada CLI (`python -m wallapop_intel` / script wallapop-intel)."""
    from wallapop_intel.context import settings, store

    cfg = settings()
    store()

    transport = cfg.transport
    if transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
        return
    mcp.run(
        transport=transport,
        host=cfg.http_host,
        port=cfg.http_port,
        show_banner=False,
    )
