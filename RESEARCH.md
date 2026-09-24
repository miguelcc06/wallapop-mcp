# Investigación — wallapop-intel

Fecha de verificación en vivo: **2026-09-24**. Entorno: VM de agente (IP de datacenter), Python 3.12, httpx/urllib contra `https://api.wallapop.com`. Ritmo de las pruebas: ≥1 s entre tandas y, en el cliente final, ≥500 ms.

## Decisión de cliente

Se **reimplementa** el cliente. No se depende de WallaPy (`Duccioo/WallaPy`, Apache-2.0, v0.6.6).

Motivos comprobados:

- WallaPy pagina con `start_cursor`. El 2026-09-24 ese parámetro **repite la primera página**. El cursor correcto es el JWT de `meta.next_page` reenviado como query `next_page` (sin solape de ids en la prueba con keyword `lego`).
- WallaPy documenta `time_filter=today|lastWeek|lastMonth`. Esos parámetros, y también `publication_date` y `since`, **no cambian** el primer anuncio ni el tamaño de página. El filtro temporal se aplica en local sobre `created_at`.
- Hace falta control explícito de ritmo, proxy, errores accionables y de no firmar salvo que se pida.

WallaPy se usó como mapa de endpoints (`/search`, `/users/{id}`, headers `X-DeviceOS: 0`, órdenes `newest`, `price_low_to_high`, `price_high_to_low`).

## API oficial Wallapop Connect — descartada

Fuentes: [developers.wallapop.com](https://developers.wallapop.com/pages/guides/listing.md), OpenAPI público de Items en `api-evangelist/wallapop` (host `https://connect.wallapop.com`).

- OAuth 2.0 Authorization Code + PKCE en `iam.wallapop.com` (realm `wallapop-connect`).
- Recursos de vendedor: items propios, transacciones, webhooks. **No hay búsqueda de mercado ni métricas de terceros.**
- Funciones como envío gratis o activar stock piden suscripción Wallapop PRO.
- No hay un modo gratuito de búsqueda. **No se implementa nada de Connect.**

## Headers que funcionan (2026-09-24)

Desde esta VM, `GET /api/v3/search` y `GET /api/v3/categories` respondieron **200** con:

- navegador + `Origin` + `Referer` + `X-DeviceOS: 0` + `Accept-Language: es-ES`
- `User-Agent: Wget/1.21.4` + `X-DeviceOS: 0`
- UA de app Android + `X-DeviceOS: 1`
- solo `User-Agent` de navegador, sin `X-DeviceOS`
- incluso `User-Agent: python` en `/categories`

No se reprodujo el 403 de CloudFront descrito para otras IPs de datacenter. El cliente igual manda el conjunto de navegador (`Origin`, `Referer`, `X-DeviceOS: 0`, `Accept-Language`, UA de escritorio rotatorio) porque es el que usa la web y el que otros clientes necesitaron en 2026-02 ([inlanger/wallaparser](https://github.com/inlanger/wallaparser)). Si llega un 403, el error pide `WALLAPOP_PROXY`.

`order_by=most_relevance` y `order_by=closest` → 200. `order_by=relevance` → **400**.

## Firma X-Signature

Referencias públicas: [rmonvfer/wallapop_secret](https://github.com/rmonvfer/wallapop_secret) (clave de cliente web, texto de empleo en Base64) y el cliente Go `krymov/wallapopped` (el repo no estaba accesible el día de la prueba; el algoritmo usado es el del cliente web publicado).

Algoritmo opcional implementado: `Base64(HMAC_SHA256(clave_publica, "METHOD|path?query|timestamp|"))`.

**No se envía por defecto.** Las lecturas de búsqueda, ficha, usuario, stats, reviews y categorías funcionaron sin `Timestamp` ni `X-Signature`. Activarla puede provocar 400. Queda detrás de `WALLAPOP_SIGN_REQUESTS=0`.

## Endpoints leídos y campos reales

Base: `https://api.wallapop.com/api/v3`.

| Método | Ruta | Resultado |
| --- | --- | --- |
| GET | `/search` | 200. `data.section.payload.items` (40). `meta.next_page` JWT. `stats.filters_applied_count` no es el total del mercado |
| GET | `/search/components` | 200. Incluye `search_id`. No hace falta para listar |
| GET | `/search/section` | 200 si se pasa `search_id`. Items en `data.section.items` |
| GET | `/items/{id}` | 200. Ver counters abajo |
| GET | `/categories` | 200. 18 raíces |
| GET | `/users/{id}` | 200. Perfil público. No trae la nota |
| GET | `/users/{id}/stats` | 200. `rating_average`, contadores |
| GET | `/users/{id}/items` | 200. Solo publicados visibles. `meta` vacío en la prueba |
| GET | `/users/{id}/reviews` | 200. Lista de reseñas |
| GET | `/users/{id}/published` y `/items/{id}/detail` | 404 |

### Anuncio en búsqueda

Claves: `id`, `user_id`, `title`, `description`, `category_id`, `price.amount`, `price.currency`, `images`, `reserved`, `location` (city, postal_code, lat/long), `shipping`, `favorited.flag` (si **tú** lo tienes en favoritos, no el contador), `bump`, `web_slug`, `created_at` y `modified_at` en **milisegundos**, `taxonomy`, `is_favoriteable`, `is_refurbished`, `is_top_profile`, `has_warranty`.

No hay `views` ni número de favoritos en el listado.

### Ficha `GET /items/{id}`

Claves verificadas: `id`, `title.original`, `description.original`, `counters`, `characteristics`, `characteristics_details`, `favorited`, `hashtags`, `images`, `in_demand_signal`, `location`, `modified_date` (**segundos**), `price.cash.amount`, `share_url`, `shipping`, `slug`, `supports_shipping`, `taxonomy`, `type`, `type_attributes` (brand, model, condition, storage…), `user.id`.

`counters` real, ejemplo del anuncio `w67v2xx3896x` («Juegos PS5»):

```json
{"views": 720, "favorites": 16, "conversations": 15}
```

El mismo anuncio en el HTML público (`__NEXT_DATA__` de `es.wallapop.com/item/...`) mostró `views: 720` y `favorites: 16`. **Coinciden.** `in_demand_signal` era `{"is_visible": true}`.

Anuncios recién publicados pueden traer ceros reales (ejemplo `w67v0m1r796x`: 0/0/0). Un cero no dispara la heurística: el método sigue siendo `api_counters` con fiabilidad alta.

### Vendedor

`/users/nzx5y8xd2562`: `micro_name`, `type`, `register_date` (ms), `seller_type.verified`, `location`, `web_slug`. El campo `gender` existe y **no se expone** en la tool.

`/stats` del mismo usuario: `rating_average` 4.9, `ratings[{type:reviews,value:97}]`, counters `publish`, `buys`, `sells`, `reviews`, `sold`, `reports_received`.

### Categorías (raíces, 2026-09-24)

18 raíces. Ids estables usados en evaluación: Coches `100` (icono `car`), Inmobiliaria `200` (`house`), Motos `14000`, Motor y accesorios `12800` (5 hijas), Tecnología y electrónica `24200` (`robot`, 6 hijas), Deporte y ocio `12579` (`ball`, 16 hijas), Servicios `13200` (8 hijas), Empleo `21000`, Otros `12485` (`ghost`). «Videojuegos» es `10093`, padre `24131`.

## Scraping HTML

Viable y, en el anuncio comparado, **redundante**: el DOM/`__NEXT_DATA__` repite los counters de la API. Queda **apagado** (`WALLAPOP_ENRICH_HTML=0`). No se añade Playwright. Si en el futuro `counters` desaparece, el siguiente paso es parsear `__NEXT_DATA__` con el mismo rate limit, no un navegador headless.

## Heurística

Documentada en `src/wallapop_intel/insights.py`.

- Con counters: `score = min(100, 8*favoritos + 0.35*visitas + 12*conversaciones)`. Fiabilidad alta. No inventa visitas.
- Sin counters: score bajo por fotos, bump, perfil destacado y antigüedad. Fiabilidad baja. Texto explícito de estimación.
- Oportunidad: precio ≤ mediana × 0,78 en la muestra.
- Ganador: grupos con las 3 primeras palabras útiles del título, mínimo 3 anuncios, demanda media y dispersión de precio.
- «Vendido»: solo si un id estaba en un snapshot anterior y no sale en la muestra nueva. Confianza baja. No es un hecho.

## Prueba en vivo de tools (2026-09-24)

Cliente MCP in-process (`fastmcp.Client` contra el servidor):

1. `wp_get_categories` query `Coches` → ids 100, 10167 (padre 12800), 10075 (padre 18000).
2. `wp_search_items` `lego star wars` limit 3 → anuncio `w67v0m1r796x`, 3 EUR, Málaga, `has_more=true`.
3. `wp_get_item` de ese id → counters 0/0/0, método `api_counters`, URL `https://wallapop.com/item/minifigura-lego-star-wars-palpatine-compatible-1305503052`.
4. `wp_item_metrics` → misma lectura, fiabilidad high.
5. `wp_get_seller` `9nz00q7l87zo` → micro_name «Play-Go ..», nota 5.0, 400 reseñas, 342 publicados, 626 vendidos, alta 2016-02-28, verificado.
6. `wp_rank_by_engagement` y `wp_market_analysis` sobre `lego palpatine` → 1 anuncio, mediana 3 EUR.

Tests unitarios: `pytest` 6 passed. `python -m py_compile` sobre el paquete.

## Mitigaciones

- Token bucket a ≤1 req/s, suelo 500 ms, jitter, 3 reintentos, `Retry-After` en 429/5xx.
- Proxy opcional.
- UA rotatorio de navegador.
- SQLite con deduplicación por anuncio y minuto, purga a 90 días.
- Caché de categorías 24 h.
- Sin logs de cuerpos con datos de más; sin tokens porque no hay login.

## Trabajo futuro (no implementado a propósito)

- Extractores autenticados de pedidos y chats (`AspiranteD/wallapop-data-extractors`): riesgo de bloqueo y de las condiciones de uso. Solo tendría sentido con una decisión explícita del usuario.
- Wallapop Connect para **publicar** como PRO: otro proyecto, con OAuth.
- Scraping HTML si Wallapop deja de mandar `counters` en la ficha.
- Total real de resultados: la API de búsqueda no lo da en `stats`.
- Filtro temporal de servidor: hoy se ignora; habría que capturarlo del cliente web si cambia el contrato.
