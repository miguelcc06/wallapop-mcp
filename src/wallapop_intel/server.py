"""Entrada del servidor MCP wallapop_mcp (producto wallapop-intel)."""

from __future__ import annotations

import logging
import sys

from fastmcp import FastMCP

from wallapop_intel.context import settings
from wallapop_intel.tools import register_all

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

mcp = FastMCP(
    "wallapop_mcp",
    instructions=(
        "Inteligencia de mercado de Wallapop en solo lectura. "
        "Las visitas y favoritos de la ficha (wp_get_item / wp_item_metrics) son reales. "
        "El listado de búsqueda no los trae. El histórico de precios solo existe desde el primer snapshot local. "
        "No hay tools de compra, publicación ni mensajes."
    ),
)
register_all(mcp)


def main() -> None:
    cfg = settings()
    if cfg.transport in {"http", "streamable-http"}:
        mcp.run(transport="streamable-http", host=cfg.http_host, port=cfg.http_port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
