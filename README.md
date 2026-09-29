# Wallapop Intel MCP

Servidor MCP para inteligencia de mercado de Wallapop (España) en modo solo lectura.

- **Tools de mercado**: búsqueda, ficha con counters reales, ranking por engagement, mediana y detección de oportunidades/ganadores.
- **Lotes y rentabilidad**: `wp_search_lots` y `wp_estimate_profit` con soporte de envíos nacionales e internacionales (Portugal, Italia) y costes de adquisición.
- **Persistencia**: Base de datos **PostgreSQL** para snapshots históricos, watchlist local y caché de categorías/métricas.

## Configuración

En tu `.env` o variables de entorno:

| Variable | Descripción | Por defecto |
|---|---|---|
| `WALLAPOP_DATABASE_URL` | DSN de conexión a PostgreSQL | `postgresql://miguelcc06@localhost:5432/wallapop_intel` |
| `WALLAPOP_USER_ID` | Tu ID de usuario en Wallapop | `""` |
| `WALLAPOP_SNAPSHOT_TTL_DAYS` | Días de retención para snapshots | `90` |
| `WALLAPOP_REQ_INTERVAL` | Intervalo mínimo entre peticiones (s) | `1.0` |
| `WALLAPOP_MAX_RETRIES` | Reintentos ante 429/5xx | `3` |

## Herramientas principales

- `wp_search_items`: Búsqueda de artículos con filtros y paginación (`cursor`).
- `wp_get_item` / `wp_item_metrics`: Ficha de artículo con visitas, favoritos y conversaciones reales (`api_counters`).
- `wp_search_lots`: Búsqueda de lotes, torres, packs y despieces con puntuación de oportunidad.
- `wp_estimate_profit`: Cálculo de rentabilidad, desglose de adquisición, envíos y ROI.
- `wp_market_analysis`: Mediana y volumen de precios de una keyword frente a históricos locales.
- `wp_opportunities`: Detección de anuncios por debajo del precio de mercado.
- `wp_watchlist_add` / `wp_watchlist_list` / `wp_watchlist_snapshot`: Vigilancia de artículos y keywords en PostgreSQL.
