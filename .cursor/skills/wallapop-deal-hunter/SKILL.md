---
name: wallapop-deal-hunter
description: Identifica chollos líquidos de informática, gaming, consolas y electrónica en Wallapop (España) con el MCP wallapop_mcp. Descarta averiados, sin envío, pines rotos y vendedores sin reputación, y solo aprueba si wp_estimate_profit devuelve passes tras un coste de compra + envío + protección. Usar cuando pidan cazar chollos, oportunidades, reventa, alta rotación o margen en Wallapop.
---

# Cazador de oportunidades

Servidor `wallapop_mcp` (paquete `wallapop_intel`). Cada tool recibe un objeto `params`. El servidor ya limita a 1 petición por segundo: no lances fichas en paralelo. No compra ni publica.

Objetivo: anuncios de alta rotación cuyo coste real (precio + envío + protección) deja un neto mayor que el 30 % de ROI o mayor que 20 EUR, después de tirar lo que no se puede revender por envío.

## Verticales

Una keyword por tanda. No mezcles consola y juegos en el mismo texto.

| Vertical | keywords | Semilla de categoría |
| --- | --- | --- |
| Portátiles | `portátil` o un modelo (`thinkpad t480`) | Tecnología `24200` |
| GPU | un chip (`rtx 3070`), no `gráfica` | `24200` |
| Consolas | `ps5` o `switch oled` | Videojuegos `10093` |
| Móviles | modelo y capacidad (`iphone 13 128`) | `24200` |
| SSD | `ssd 1tb` | `24200` |

Esas ids salen del árbol del 2026-09-24. Si la búsqueda vuelve vacía, confirma con `wp_get_categories` antes de repetir. No pongas `distance_km`: el chollo tiene que admitir envío, no quedarse en un radio local. `order_by=relevance` no existe; el valor es `most_relevance`.

## Puerta

`sell_price_eur` es la mediana de `wp_market_analysis`, no el precio del anuncio y no un precio recordado. Aprueba solo si `passes` es true.

- `reliability=high` (`checkout_observed`): envío y protección salieron del checkout.
- `reliability=low` (`reference_ceiling_v1`): el servidor usó un techo no oficial. Si `passes` es true, el candidato sigue siendo válido y el ROI real no debería ser peor. Si `passes` es false, no lo mates: falta el importe del checkout. No inventes otra tarifa.

Igualar el 30 % o los 20 EUR no pasa. Lee `passes_roi` y `passes_net`. No recalcules el ROI con el texto redondeado del `summary`.

Tramo si la ficha no trae peso (embalaje incluido):

| Bulto | `weight_bracket` |
| --- | --- |
| Mando, juego, SSD, RAM suelta | `up_to_2kg` |
| Portátil, consola, móvil | `up_to_5kg` |
| Torre pequeña o monitor | `up_to_10kg` |
| Torre gaming | `up_to_20kg` |

Por encima de 30 kg o de 120 cm no hay tramo: pasa `bulky_fee_eur=4.5` y no uses `weight_kg` mayor que 30. Entrega en punto: `home_pickup_eur=0`.

## Descarte

Tira el anuncio en cuanto se cumpla una condición. No lo «rescates» por el descuento.

- `reserved` true, `price_eur` null, o `currency` distinta de EUR.
- `shippable` false. Si es null, lee la descripción: «sin envío», «no envío», «solo en mano», «recoger en mano» también descartan.
- Texto de avería, en título o descripción: averiad, no funciona, no enciende, para piezas, despiece, pin roto, pines rotos, no da imagen, pantalla rota, bloqueado, icloud, cuenta de google, no carga, se apaga.
- Vendedor: `sold` es 0 y `reviews` es 0. O `rating_average` < 4 con `reviews` >= 3. Si `wp_get_seller` falla, no apruebes ese anuncio.

Las visitas del listado no existen. Para citar visitas o favoritos hace falta `wp_get_item` o `wp_item_metrics` con `counters_are_real` / `reliability=high`. Un cero real sigue siendo un cero.

## Flujo

1. **Mercado.** `wp_market_analysis` con la keyword, `limit` 30 y `category_id` si ya lo confirmaste. Para si `sample_size` < 8 o `median_price` es null. Guarda mediana, `p25` y `p75`. Ignora `newest_per_day_estimate`.
2. **Bajo la mediana.** `wp_opportunities` con las mismas keywords y `max_results` 8. El servidor ya exige precio <= mediana × 0,78. Una lista vacía cierra la tanda: no rebajes el umbral a mano.
3. **Ficha.** Por cada fila, de mayor `score` a menor, `wp_get_item`. Aplica el descarte. Máximo 8 fichas.
4. **Vendedor.** `wp_get_seller` con el `user_id` de la ficha. Máximo 4 vendedores. Aplica el descarte de reputación.
5. **Margen.** `wp_estimate_profit` con `buy_price_eur` = precio del anuncio, `sell_price_eur` = mediana del paso 1, el tramo de la tabla y `shipping_eur` / `protection_eur` solo si el usuario los ha leído en el checkout.
6. **Entrega.** Como mucho los 3 primeros con `passes` true: id, url, ciudad, precio, mediana, `net_eur`, `roi`, `reliability` y la señal de envío (`shippable`).

## Parada

Para en el primer tope, aunque queden filas: 3 aprobados, 8 fichas, 4 vendedores, muestra < 8, oportunidades vacías, o un 403. Ante un 403 no reintentes en bucle: hace falta `WALLAPOP_PROXY` o esperar. No pidas una segunda página.

## Ejemplo

```json
{"params": {"keywords": "ps5 slim", "category_id": 10093, "limit": 30}}
```

`wp_market_analysis`. Supón mediana 320 y `sample_size` 18.

```json
{"params": {"keywords": "ps5 slim", "category_id": 10093, "limit": 30, "max_results": 8}}
```

`wp_opportunities`. Para el id `w67v2xx3896x`:

```json
{"params": {"item_id": "w67v2xx3896x"}}
```

`wp_get_item`. Si `shippable` es true, no está reservado y la descripción no trae el léxico de avería:

```json
{"params": {"user_id": "nzx5y8xd2562"}}
```

`wp_get_seller`. Con nota y ventas por encima del descarte:

```json
{"params": {"buy_price_eur": 240, "sell_price_eur": 320, "weight_bracket": "up_to_5kg", "shipping_eur": 6.9, "protection_eur": 4.5, "min_roi": 0.3, "min_net_eur": 20}}
```

`wp_estimate_profit`. Publica el candidato solo si `passes` es true. Si omites `shipping_eur` y `protection_eur`, dilo en la respuesta: la cifra es techo, fiabilidad baja.
