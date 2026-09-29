"""Registro de todas las tools wp_*."""

from fastmcp import FastMCP

from wallapop_intel.tools import catalog, lots, market, mine, profit, ranking, watch


def register_all(mcp: FastMCP) -> None:
    catalog.register(mcp)
    ranking.register(mcp)
    market.register(mcp)
    lots.register(mcp)
    profit.register(mcp)
    watch.register(mcp)
    mine.register(mcp)
