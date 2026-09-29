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
        "wp_watchlist_list consulta la SQLite local. "
        "No hay tools de compra, publicación ni mensajes."
    ),
)

register_all(mcp)
