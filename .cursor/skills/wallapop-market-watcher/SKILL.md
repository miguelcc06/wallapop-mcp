---
name: wallapop-market-watcher
description: Alimenta y consulta la watchlist local de Wallapop, detecta bajadas de precio de la competencia y estima la velocidad a la que los anuncios salen de la muestra. Usar cuando pidan vigilar, alertas, seguimiento, watchlist, bajadas de precio o velocidad de venta con wp_watchlist_list, wp_watchlist_add, wp_watchlist_snapshot, wp_price_history o wp_market_analysis.
---

# Monitorización

Servidor `wallapop_mcp`. La watchlist vive en SQLite (`WALLAPOP_DB_PATH`). `wp_watchlist_add`, `wp_watchlist_remove` y `wp_watchlist_list` no llaman a Wallapop. `wp_watchlist_snapshot` sí lee la red y guarda la captura. El histórico no existe antes de la primera captura: una serie vacía no es un precio plano.

`wp_watchlist_snapshot` procesa como máximo 8 entradas por llamada, en el orden en que las devuelve la lista. Si hay más, las demás no se han leído. No digas que toda la lista está actualizada.

## Qué vigilar

- `kind=item` y `key` = id del anuncio (`w67v2xx3896x`) para un precio concreto.
- `kind=keyword` y `key` = la misma keyword que usarías en `wp_market_analysis` (`ps5 slim`, no una frase).
- `label` corta: ciudad o el motivo (`chollo`, `competencia`).

No des de alta un catálogo que el usuario no haya pedido. Si la lista está vacía, pregunta qué id o qué keyword vigilar y para.

## Bajada

Una bajada es real solo si `wp_market_analysis` trae `price_drops` o `wp_price_history` muestra dos precios del mismo `item_id`. El primer análisis deja `price_drops` y `disappeared` vacíos: eso significa que aún no había snapshot, no que el mercado esté quieto.

Alerta si la caída es de al menos 10 EUR o de al menos el 8 % del precio anterior. Cita `from_price`, `to_price` y `captured_before`. No alertes por diferencias de céntimos.

## Velocidad

`disappeared` es confianza baja: el anuncio puede haberse vendido, reservado fuera de la página o cambiado de título. No lo llames venta.

Con dos capturas del mismo keyword en `wp_price_history`:

`ausencias_por_dia = número de disappeared / días entre la captura anterior y la actual`

Si los días son 0, no dividas. Publica el ratio como «ausencias en la muestra por día», con confianza baja. Ignora `newest_per_day_estimate`: el servidor lo devuelve vacío.

## Flujo

1. `wp_watchlist_list`.
2. Alta solo de lo pedido, con `wp_watchlist_add`. Repetir la misma clave actualiza la etiqueta y devuelve la lista entera.
3. Una sola `wp_watchlist_snapshot` por petición.
4. Para cada id o keyword que el snapshot haya cubierto, `wp_price_history` con `limit` 30. Pasa `item_id` o `keywords`, no los dos vacíos.
5. Si la entrada es una keyword, un `wp_market_analysis` (`limit` 30) para leer `price_drops`. No lo lances para un `kind=item`: el histórico de ese id ya basta.
6. Entrega: lista vigilada, cuáles de las 8 se capturaron, alertas que superan el umbral y, si hay dos fechas, las ausencias por día.

Quita con `wp_watchlist_remove` cuando el usuario lo pida o cuando `wp_get_item` responda que el anuncio ya no está (404: vendido o id malo). Los snapshots ya guardados no se borran.

## Parada

Lista vacía y nada que añadir: para. Más de 8 entradas: dilo y no lances un segundo snapshot en la misma petición. Un 403: para, sin reintentos en bucle. No conviertas una ausencia en una venta cerrada.

## Ejemplo

```json
{"params": {}}
```

`wp_watchlist_list`. Si no está la keyword:

```json
{"params": {"kind": "keyword", "key": "ps5 slim", "label": "competencia"}}
```

`wp_watchlist_add`.

```json
{"params": {"kind": "item", "key": "w67v2xx3896x", "label": "candidato"}}
```

```json
{"params": {}}
```

`wp_watchlist_snapshot`. Luego:

```json
{"params": {"keywords": "ps5 slim", "limit": 30}}
```

`wp_price_history` y `wp_market_analysis` con ese mismo `params`. Una fila de `price_drops` de 350 a 310 EUR (40 EUR, más del 8 %) es alerta. Una fila de `disappeared` se cuenta para el ratio y se etiqueta como no confirmada.

Para dejar de vigilar el anuncio:

```json
{"params": {"kind": "item", "key": "w67v2xx3896x"}}
```

`wp_watchlist_remove`.
