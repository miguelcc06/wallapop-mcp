---
name: wallapop-lot-flipper
description: Busca lotes, packs, torres de oficina o gaming y despieces en Wallapop, tasa cada componente con la mediana de la muestra y compara el neto de vender por piezas frente a revender el equipo montado. Usar cuando pidan lotes, packs, torres, despiece, piezas sueltas o rentabilidad de un lote con wp_search_lots, wp_market_analysis y wp_estimate_profit.
---

# Lotes y despiece

Servidor `wallapop_mcp`. Argumento único `params`. Una búsqueda de más por tanda, no varias en paralelo. No compra ni publica.

`wp_search_lots` añade `lote`, `pack`, `despiece` o `urgente` si la keyword no lo trae, y se queda con anuncios cuyo título o descripción contiene la señal. Un lote mal titulado no aparece. No descarta reservados ni «sin envío»: eso lo haces tú en la ficha.

## Qué buscar

| Objetivo | keywords | focus |
| --- | --- | --- |
| Torre de oficina | `torre oficina` o `pc i5` | `lot` |
| Torre gaming | `pc gaming` o `torre rtx` | `lot` |
| Pack de componentes | `ram ddr4` o `ssd` | `pack` |
| Equipo para piezas | `pc` o el chip | `teardown` |
| Salida rápida del vendedor | el producto | `urgent` |

Categoría semilla de electrónica: `24200`. Confírmala con `wp_get_categories` si la página viene vacía. Sin `distance_km`.

## Tasación

Saca de la ficha solo piezas con modelo o capacidad escritos (placa, CPU, RAM con GB y generación, GPU, fuente con vatios, almacenamiento). Si no está escrito, esa pieza no se tasa. No completes la spec de memoria.

Por cada pieza, `wp_market_analysis` con una keyword cerrada (`i5 10400`, `rtx 3070`, `16gb ddr4`). La mediana vale solo si `sample_size` >= 8 y `median_price` no es null. Hacen falta al menos 3 piezas válidas para hablar de despiece. Chasis y cables no se tasan: no tienen mercado estable en el título.

Política de liquidez de esta skill, no un dato de Wallapop: `valor_piezas_liquido = suma de medianas × 0,85`. Redondea a céntimos. Dilo en la respuesta.

El equipo montado se tasa con otra `wp_market_analysis` cuya keyword describa el conjunto real (`pc i5 10400 16gb`), no la palabra `lote`. Misma regla de muestra >= 8.

## Dos cuentas

`wp_estimate_profit` modela una compra y una venta. El comprador de cada pieza paga su envío: no lo restes del ingreso. El embalaje de salir sí es tuyo.

1. **Despiece.** `buy_price_eur` = precio del lote. `sell_price_eur` = `valor_piezas_liquido`. `weight_bracket` del bulto que compras (`up_to_20kg` en una torre). `packaging_eur` = 2 × número de piezas tasadas. `home_pickup_eur` = 0 salvo que el usuario vaya a pedir recogida y te diga el importe.
2. **Montado.** El mismo `buy_price_eur` y el mismo tramo. `sell_price_eur` = mediana del conjunto. `packaging_eur` = 3.

Pasa `shipping_eur` y `protection_eur` solo si están vistos en el checkout. Si faltan, fiabilidad baja: un `passes` true es un techo conservador; un false no cierra el lote.

Recomienda el camino con mayor `net_eur` entre los que tengan `passes` true. Si ninguno pasa, el lote no se compra. No declares ganador un camino con menos de 3 piezas o con mediana inválida.

## Flujo

1. `wp_search_lots` con `limit` 15, `order_by` `newest` y el `focus` de la tabla.
2. `wp_get_item` de cada hit, como máximo 5. Tira `reserved`, `shippable` false y «sin envío / solo en mano».
3. Lista las piezas identificables. Si no llegas a 3, di que el anuncio no se puede despiezar con datos y pasa al siguiente. No rellenes huecos.
4. Una `wp_market_analysis` por pieza (máximo 6) y una del conjunto montado.
5. Las dos llamadas a `wp_estimate_profit`.
6. Entrega una ficha: url, precio, piezas con mediana y `sample_size`, neto de cada camino, `reliability` y el camino elegido.

## Parada

3 lotes tasados, o 5 fichas, o 6 análisis de pieza en el lote en curso, o `hits` vacío. Un 403 para la tanda. No pases `cursor` más de una vez, y solo si el usuario pidió más página y `next_cursor` viene informado.

## Ejemplo

```json
{"params": {"keywords": "torre oficina", "focus": "lot", "category_id": 24200, "limit": 15, "order_by": "newest"}}
```

`wp_search_lots`. La query efectiva será `torre oficina lote` si el texto no contenía ya «lote».

```json
{"params": {"item_id": "w67v2xx3896x"}}
```

`wp_get_item`. Descripción con i5-10400, 16 GB DDR4 y sin GPU dedicada: tres piezas no, porque faltaría la tercera spec escrita. No sigas. Si además hay un SSD de 512 GB identificado:

```json
{"params": {"keywords": "i5 10400", "category_id": 24200, "limit": 20}}
```

Repite el análisis para `16gb ddr4` y `ssd 512`. Supón medianas 55, 28 y 22, todas con muestra >= 8. Suma 105. Líquido `105 × 0,85 = 89,25`.

```json
{"params": {"keywords": "pc i5 10400 16gb", "category_id": 24200, "limit": 20}}
```

Supón mediana montada 120 y compra del lote a 40 EUR, checkout aún no visto:

```json
{"params": {"buy_price_eur": 40, "sell_price_eur": 89.25, "weight_bracket": "up_to_20kg", "packaging_eur": 6, "min_roi": 0.3, "min_net_eur": 20}}
```

```json
{"params": {"buy_price_eur": 40, "sell_price_eur": 120, "weight_bracket": "up_to_20kg", "packaging_eur": 3, "min_roi": 0.3, "min_net_eur": 20}}
```

Elige el `net_eur` mayor con `passes` true y escribe que el envío de entrada es techo de referencia si `reliability` es `low`.
