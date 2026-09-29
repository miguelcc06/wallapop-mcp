---
name: wallapop-listing-optimizer
description: Optimiza títulos, palabras clave y precio de anuncios en Wallapop y diagnostica anuncios propios estancados con wp_my_items y wp_my_item_advice. Usar cuando pidan publicar, titular, palabras clave, precio psicológico, venta rápida, margen máximo o bajar un anuncio parado.
---

# Anuncios y precio

Servidor `wallapop_mcp`, solo lectura. El texto y el precio que salgan de aquí los publica la persona: no hay tool de alta ni de edición. `wp_my_items` y `wp_my_item_advice` exigen `WALLAPOP_USER_ID`. Si la tool lo pide, para y dilo. No diagnostiques un anuncio ajeno como propio: la tool rechaza un id de otro vendedor y manda a `wp_item_metrics`.

## Título

Las tres primeras palabras útiles del título son la clave con la que `wp_winning_products` agrupa la demanda. Empieza por marca, modelo y la spec que la gente busca. Ciudad, adjetivos y «oportunidad» no van delante.

- Forma: `Marca Modelo spec estado corto`.
- Unas 50 caracteres. Sin mayúsculas enteras, sin emojis, sin «chollo», «impecable» ni «urge» como primera palabra.
- Una spec comprobable: capacidad, chip, tamaño, batería si está medida.

| Flojo | Título |
| --- | --- |
| PS5 en perfecto estado Madrid!!! | `PS5 Slim lectora 1 TB blanca` |
| Móvil apple oportunidad | `iPhone 13 128 GB negro` |
| Gráfica gaming top | `RTX 3070 8 GB dual` |

Después de redactar, una sola comprobación: `wp_search_items` con ese título, `order_by` `most_relevance`, `limit` 5. Si los cinco resultados son otro producto, reescribe. No pagines.

## Precio

Dos modos. Elige uno y no mezcles la cifra.

**Venta rápida.** A partir de la mediana (`market_median` de `wp_my_item_advice`, o `median_price` de `wp_market_analysis` si necesitas `p75`). El mayor entero <= mediana que acabe en 0 o en 5. Por debajo de 100 EUR, si un precio acabado en 9 sigue siendo <= mediana, usa ese (79, 99). Nunca por encima de la mediana.

**Margen máximo.** Solo con `p75` de `wp_market_analysis` de la misma keyword, `age_days` < 7 y `favorites` >= 3. Precio = `p75` redondeado hacia abajo al múltiplo de 5, sin pasar de `p75`. Si falta la edad, los favoritos o la muestra, no uses este modo: quédate en la mediana.

La mediana y el `p75` son de la muestra, no del mercado entero. Si `competitor_count` < 5 o `sample_size` < 8, no cambies el precio.

## Anuncio parado

Una bajada por pasada. El suelo es `0,80 × mediana`: por debajo no bajes.

1. `wp_my_items` con `limit` 10.
2. `wp_my_item_advice` del id que nombre el usuario, o del que el summary marque más lejos de la mediana.
3. Si `photo_count` < 3, o las `recommendations` piden descripción (estado, capacidad, accesorios, envío): el siguiente paso es contenido, no precio. Propón 4 fotos (frontal, detalle, etiqueta, defecto) y un párrafo con eso. Para.
4. Si `your_price` > `1,15 × market_median`: un único precio nuevo, el de venta rápida sobre esa mediana.
5. Si `age_days` > 14, `favorites` < 2 y el precio ya está entre el 85 % y el 115 % de la mediana: resta un 8 % y redondea al euro. Si ese resultado queda por debajo del suelo, el precio nuevo es el suelo y no hay otra bajada.
6. Si `age_days` es null, no inventes antigüedad y no apliques el paso 5.

`wp_my_item_advice` no devuelve `p75`. Para el modo de margen máximo haz un `wp_market_analysis` con los primeros 80 caracteres del título y `limit` 20.

## Parada

Un anuncio por petición. Una búsqueda de comprobación del título. Una bajada. Muestra insuficiente o usuario sin configurar: para con la causa, sin precio inventado. Un 403 para la tanda.

## Ejemplo

```json
{"params": {"limit": 10}}
```

`wp_my_items`.

```json
{"params": {"item_id": "w67v2xx3896x"}}
```

`wp_my_item_advice`. Supón precio 220, mediana 180, `age_days` 20, `favorites` 1, `photo_count` 6, `competitor_count` 14. 220 > 1,15 × 180 (207), así que toca venta rápida: el mayor múltiplo de 5 que no supera 180 es 180. Una sola propuesta, 180 EUR.

Si el precio ya fuera 175, con la misma edad y favoritos: 175 × 0,92 = 161, suelo 0,80 × 180 = 144, así que la bajada única es 161 EUR.

```json
{"params": {"keywords": "PS5 Slim lectora 1 TB blanca", "order_by": "most_relevance", "limit": 5}}
```

`wp_search_items`, solo para ver si el título nuevo recupera el mismo tipo de anuncio.
