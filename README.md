<div align="center">
  <img src="./assets/banner.png" width="100%" alt="wallapop-mcp banner" />

  <br />
  <br />

  # 🛒 wallapop-mcp

  **Servidor MCP de solo lectura: inteligencia de mercado sobre Wallapop para agentes**

  <p align="center">
    <img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+" />
    <img src="https://img.shields.io/badge/FastMCP-2.10+-00C7B7?style=for-the-badge" alt="FastMCP 2.10+" />
    <img src="https://img.shields.io/badge/PostgreSQL-16+-4169E1?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL" />
    <img src="https://img.shields.io/badge/HTTPX-0.27+-0B6F97?style=for-the-badge" alt="HTTPX 0.27+" />
    <img src="https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge" alt="MIT" />
  </p>
</div>

---

Servidor MCP (`wallapop_mcp`) de **solo lectura** para dar a un agente inteligencia de mercado sobre Wallapop en España. No publica, no compra y no envía mensajes.

La API que usa (`https://api.wallapop.com/api/v3`) **no es oficial**. Wallapop puede cambiarla o bloquearla. Úsala en personal, con el ritmo por defecto (≤1 petición/segundo) y bajo tu responsabilidad. Puede entrar en conflicto con las condiciones de Wallapop. No hay garantías de disponibilidad ni de exactitud.

## Instalación

Requiere Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
createdb wallapop_intel   # si la base aún no existe
```

La persistencia local es PostgreSQL (`snapshots`, `watchlist`, `category_cache`, `metric_cache`). La URL se resuelve en este orden:

1. `WALLAPOP_DATABASE_URL`
2. `DATABASE_URL`
3. Socket unix si existe: `postgresql://miguelcc06@/wallapop_intel?host=/var/run/postgresql` (o `/tmp`)
4. `postgresql://miguelcc06@localhost:5432/wallapop_intel`

El esquema se crea al arrancar. El usuario por defecto es `miguelcc06`; otro usuario o contraseña van en la URL.

Arranque stdio (el que espera un cliente MCP local):

```bash
PYTHONPATH=src .venv/bin/python -m wallapop_intel
```

Transporte HTTP opcional, solo en local:

```bash
WALLAPOP_TRANSPORT=http WALLAPOP_HTTP_HOST=127.0.0.1 WALLAPOP_HTTP_PORT=8000 \
  PYTHONPATH=src .venv/bin/python -m wallapop_intel
```

Ejemplo de cliente (Claude Desktop / Cursor), ajusta la ruta:

```json
{
  "mcpServers": {
    "wallapop_mcp": {
      "command": "/ruta/al/repo/.venv/bin/python",
      "args": ["-m", "wallapop_intel"],
      "env": {
        "PYTHONPATH": "/ruta/al/repo/src",
        "WALLAPOP_LATITUDE": "40.416775",
        "WALLAPOP_LONGITUDE": "-3.703790"
      }
    }
  }
}
```

## Variables de entorno

| Variable | Default | Uso |
| --- | --- | --- |
| `WALLAPOP_LATITUDE` / `WALLAPOP_LONGITUDE` | Madrid | Centro de búsqueda si la tool no recibe coordenadas |
| `WALLAPOP_USER_ID` | vacío | Id público de tu perfil. Sin él, `wp_my_items` y `wp_my_item_advice` explican cómo configurarlo |
| `WALLAPOP_PROXY` | vacío | Proxy HTTP si CloudFront responde 403 |
| `WALLAPOP_RATE_LIMIT_RPS` | `1` (techo duro) | Peticiones por segundo |
| `WALLAPOP_MIN_DELAY_MS` | `500` (suelo duro) | Pausa mínima entre peticiones |
| `WALLAPOP_DATABASE_URL` | socket unix si existe, si no `postgresql://miguelcc06@localhost:5432/wallapop_intel` | Snapshots, watchlist y caché. `DATABASE_URL` es el segundo candidato |
| `WALLAPOP_SIGN_REQUESTS` | `0` | Firma heredada. En sept-2026 no hace falta y puede romper la petición |
| `WALLAPOP_ENRICH_HTML` | `0` | Reservado. La ficha JSON ya trae visitas y favoritos |

## Tools

El argumento de cada tool es un objeto `params`. Todas devuelven JSON estructurado con un campo `summary` en Markdown.

| Tool | Qué hace |
| --- | --- |
| `wp_search_items` | Búsqueda con precio, categoría, radio, orden y paginación (`cursor` = `next_cursor`) |
| `wp_get_item` | Ficha con visitas, favoritos y conversaciones reales |
| `wp_get_categories` | Raíces del árbol, o coincidencias si pasas `query` |
| `wp_get_seller` | Perfil público y estadísticas (nota, vendidos, reseñas) |
| `wp_get_seller_items` | Anuncios publicados de un vendedor |
| `wp_item_metrics` | Contadores reales y score, con el método y la fiabilidad |
| `wp_rank_by_engagement` | Ordena una búsqueda por visitas, favoritos o score |
| `wp_market_analysis` | Mediana de la muestra y diff contra snapshots locales |
| `wp_price_history` | Serie local de un anuncio o keyword |
| `wp_opportunities` | Precios al menos un 22 % bajo la mediana de la muestra |
| `wp_search_lots` | Lotes, packs, piezas, averiados y urgencias con score compuesto |
| `wp_estimate_profit` | Margen neto, envíos Wallapop Envíos, protección comprador y ROI |
| `wp_winning_products` | Clusters de títulos con demanda en la muestra |
| `wp_watchlist_add` / `wp_watchlist_remove` | Vigilancia local |
| `wp_watchlist_snapshot` | Lee ahora lo vigilado y lo guarda |
| `wp_my_items` | Tus anuncios públicos, con pista de precio |
| `wp_my_item_advice` | Diagnóstico de un anuncio tuyo |
| `wp_search_lots` | Busca y clasifica lotes, packs, despieces y urgencias en la muestra |
| `wp_estimate_profit` | Margen neto local. Sin importes de checkout usa un techo no oficial y fiabilidad baja |
| `wp_watchlist_list` | Lista la watchlist local sin llamar a Wallapop |

`wp_estimate_profit` no consulta tarifas en vivo. Wallapop publica los tramos (hasta 2 / 5 / 10 / 20 / 30 kg) y que, si no eliges peso, se factura el de 5 kg, pero no el precio de cada tramo ni el de la Protección. Pasa `shipping_eur` y `protection_eur` vistos en el checkout para fiabilidad alta. El recargo voluminoso publicado es 4,50 EUR (`bulky_fee_eur`). La puerta por defecto pasa solo si el ROI es **mayor** que 30 % o el neto es **mayor** que 20 EUR.

## Skills del agente

Procedimientos en `.agents/skills/` (y la misma copia en `.cursor/skills/`):

| Skill | Cuándo |
| --- | --- |
| `wallapop-deal-hunter` | Chollos líquidos de informática, gaming y electrónica |
| `wallapop-lot-flipper` | Lotes, torres y despiece frente a reventa montada |
| `wallapop-listing-optimizer` | Títulos, precio y anuncios propios estancados |
| `wallapop-market-watcher` | Watchlist, bajadas y velocidad de salida de la muestra |

Ejemplo de argumentos de búsqueda:

```json
{
  "params": {
    "keywords": "iphone 13",
    "min_price": 80,
    "max_price": 350,
    "latitude": 40.416775,
    "longitude": -3.70379,
    "distance_km": 50,
    "order_by": "newest",
    "timeframe": "lastWeek",
    "limit": 10,
    "include_details": false
  }
}
```

La página siguiente usa el `next_cursor` devuelto en el campo `cursor`.

### Lotes y rentabilidad

`wp_search_lots` amplía la keyword con «lote pack piezas» (salvo que el texto ya traiga lote, pack, piezas, averiado, urge o «no enciende») y puntúa cada anuncio:

- **signal_score**: pesos del título y, en los 6 primeros, de la descripción (lote, pack, para piezas, averiado, no enciende, urge…).
- **price_score**: descuento frente a la mediana de la muestra.
- **urgency_score**: urge, mudanza, liquidación.
- **opportunity_score** = 0,45 × señal + 0,35 × precio + 0,20 × urgencia.

`wp_estimate_profit` no llama a Wallapop. Con `purchase_price`, `expected_resale_price` y `weight_kg` o `weight_band` (`under_2kg`, `kg_2_5`, `kg_5_10`, `kg_10_20`, `over_20kg`):

- Envío nacional orientativo por tramo (el extremo alto si `conservative_shipping` es true): ≤2 kg 2,95–3,95 €, ≤5 kg 4,95 €, ≤10 kg 7,95 €, ≤20 kg 9,95–12,95 €, por encima 14,95–19,95 €.
- Envío internacional / transfronterizo (`is_international`, p. ej. Portugal o Italia hacia España): ≤2 kg 5,95–6,95 €, ≤5 kg 7,95–8,95 €, ≤10 kg 11,95 €, ≤20 kg 16,95 €, por encima 24,95 €.
- Protección comprador ≈ 2,50 € + 5 % del precio de venta. Por defecto se resta como escenario conservador; en Wallapop la paga el comprador.
- Margen bruto = venta − compra. Costes = envíos + protección + `other_costs`. Coste de adquisición = compra + envío de compra + protección, si aplican. Beneficio neto = margen bruto − costes. ROI = beneficio neto / precio de compra.

Por defecto se resta el envío de la venta. `include_inbound_shipping` suma también el de la compra.

## Ritmo y errores

El cliente espera al menos 500 ms, aplica jitter y reintenta 429 y 5xx respetando `Retry-After`. No sube de 1 req/s aunque se pida más en el entorno.

- **403**: IP probablemente bloqueada. Configura `WALLAPOP_PROXY` o espera.
- **400**: `order_by=relevance` no existe; el valor válido es `most_relevance`.
- **404**: el anuncio ya no está. Puede haberse vendido o el id es incorrecto.

## Qué es real y qué es estimado

- **Real** (fiabilidad `high`, método `api_counters`): `views`, `favorites` y `conversations` de `GET /items/{id}`.
- **Estimado** (fiabilidad `low`, método `heuristic_v1`): el score cuando la ficha no trae counters. La búsqueda en listado no los trae; por eso el ranking pide fichas con un tope.
- **Histórico**: solo existe desde el primer snapshot en PostgreSQL. Una ausencia en la muestra no demuestra una venta.
- **Lotes y rentabilidad**: `opportunity_score`, el envío y la protección son estimaciones. No son el importe del checkout.

Detalle de la verificación en [RESEARCH.md](RESEARCH.md).

## Tests

```bash
PYTHONPATH=src .venv/bin/python -m pytest
PYTHONPATH=src .venv/bin/python -m py_compile src/wallapop_intel/*.py src/wallapop_intel/tools/*.py
```

Los tests de persistencia abren un esquema temporal en PostgreSQL y se omiten si la base no está accesible, para no romper un CI sin Postgres. La resolución de `WALLAPOP_DATABASE_URL` se prueba sin conectar.

## Aviso legal

Proyecto personal y educativo. No está afiliado a Wallapop. No uses los datos para spam, reventa automatizada ni para eludir límites de la plataforma. Si un endpoint falla de forma estable, reduce la frecuencia en lugar de insistir.
