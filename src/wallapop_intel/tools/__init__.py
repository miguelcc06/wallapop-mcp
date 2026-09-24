"""Registro de todas las tools wp_*."""

from fastmcp import FastMCP

from wallapop_intel.tools import catalog, market, mine, ranking, watch


def register_all(mcp: FastMCP) -> None:
    catalog.register(mcp)
    ranking.register(mcp)
    market.register(mcp)
    watch.register(mcp)
    mine.register(mcp)
