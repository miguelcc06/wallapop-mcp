"""Búsqueda, ficha, categorías y vendedores."""

from __future__ import annotations

from fastmcp import FastMCP

from wallapop_intel.context import client, store
from wallapop_intel.insights import apply_engagement, engagement
from wallapop_intel.models import (
    CategoriesInput,
    CategoriesResponse,
    CategoryNode,
    ItemDetailResponse,
    ItemIdInput,
    SearchInput,
    SearchResponse,
    SellerInput,
    SellerItemsInput,
    SellerItemsResponse,
    SellerResponse,
)
from wallapop_intel.normalize import description_of, epoch_to_iso, to_card
from wallapop_intel.tools.common import bullets, cards_from_search, fail, snapshot_card

_READ = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


def register(mcp: FastMCP) -> None:
    @mcp.tool(name="wp_search_items", title="Buscar anuncios", annotations=_READ)
    async def wp_search_items(params: SearchInput) -> SearchResponse:
        """Busca anuncios públicos en Wallapop (España) y devuelve una página.

        Filtros: keywords, precio, category_id, lat/long, radio, orden y timeframe
        local (today, lastWeek, lastMonth) porque la API ignora time_filter.
        Paginación: pasa next_cursor en la siguiente llamada como cursor.
        include_details pide la ficha (visitas y favoritos reales) de hasta 8 anuncios.

        Returns:
            SearchResponse con items, has_more, next_cursor y summary en markdown.
        """
        try:
            payload = await client().search(
                params.keywords,
                latitude=params.latitude,
                longitude=params.longitude,
                min_price=params.min_price,
                max_price=params.max_price,
                category_id=params.category_id,
                order_by=params.order_by.value,
                distance_km=params.distance_km,
                cursor=params.cursor,
            )
            cards, cursor = cards_from_search(payload, params.timeframe.value, params.limit)
            if params.include_details:
                detailed = []
                for card in cards[:8]:
                    raw = await client().get_item(card.id)
                    rich = apply_engagement(to_card(raw))
                    rich = rich.model_copy(update={"user_id": card.user_id or rich.user_id, "city": rich.city or card.city})
                    detailed.append(rich)
                    snapshot_card(rich, params.keywords, rich.metrics_method, rich.reliability)
                cards = detailed + cards[8:]
            else:
                for card in cards:
                    snapshot_card(card, params.keywords, "search_listing", "medium")
            summary = (
                f"# Búsqueda «{params.keywords}»\n\n"
                f"{len(cards)} anuncios · orden {params.order_by.value} · "
                f"{'hay más páginas' if cursor else 'sin página siguiente'}.\n\n"
                + bullets(cards)
            )
            return SearchResponse(
                summary=summary,
                keywords=params.keywords,
                count=len(cards),
                has_more=bool(cursor),
                next_cursor=cursor,
                items=cards,
                note=(
                    "Los listados de búsqueda no traen el contador de visitas. "
                    "include_details o wp_get_item leen counters reales. "
                    "El timeframe se aplica en local sobre created_at."
                ),
            )
        except Exception as exc:
            fail(exc)

    @mcp.tool(name="wp_get_item", title="Ficha de anuncio", annotations=_READ)
    async def wp_get_item(params: ItemIdInput) -> ItemDetailResponse:
        """Devuelve la ficha pública de un anuncio, incluidos counters reales.

        Campos verificados en GET /api/v3/items/{id}: precio, descripción,
        counters.views, counters.favorites, counters.conversations, ubicación,
        fotos y vendedor. No hay escritura.

        Returns:
            ItemDetailResponse. counters_are_real es true si la API envió counters.
        """
        try:
            raw = await client().get_item(params.item_id)
            card = apply_engagement(to_card(raw))
            score, reliability, method, explanation = engagement(card)
            card = card.model_copy(update={"engagement_score": score, "reliability": reliability, "metrics_method": method})
            snapshot_card(card, None, method, reliability)
            chars = raw.get("characteristics")
            char_text = chars.get("text") if isinstance(chars, dict) else None
            demand = raw.get("in_demand_signal")
            visible = demand.get("is_visible") if isinstance(demand, dict) else None
            summary = (
                f"# {card.title} ({card.id})\n\n"
                f"- Precio: {card.price_eur} {card.currency or 'EUR'}\n"
                f"- Ciudad: {card.city or 'n/d'}\n"
                f"- Visitas: {card.views if card.views is not None else 'no expuesto'} · "
                f"Favoritos: {card.favorites if card.favorites is not None else 'no expuesto'} · "
                f"Conversaciones: {card.conversations if card.conversations is not None else 'n/d'}\n"
                f"- Fiabilidad: {reliability} ({method})\n"
                f"- URL: {card.url or 'n/d'}\n\n{explanation}"
            )
            return ItemDetailResponse(
                summary=summary,
                item=card,
                description=description_of(raw),
                condition=(raw.get("type_attributes") or {}).get("condition", {}).get("text")
                if isinstance(raw.get("type_attributes"), dict) and isinstance(raw["type_attributes"].get("condition"), dict)
                else char_text,
                characteristics=char_text,
                in_demand_visible=visible,
                counters_are_real=card.views is not None or card.favorites is not None,
                raw_field_names=sorted(raw.keys()),
            )
        except Exception as exc:
            fail(exc)

    @mcp.tool(name="wp_get_categories", title="Árbol de categorías", annotations=_READ)
    async def wp_get_categories(params: CategoriesInput) -> CategoriesResponse:
        """Lista categorías de Wallapop (caché local de 24 h) y permite filtrar por nombre.

        Returns:
            CategoriesResponse con id, nombre, padre e icono. Aplana el árbol.
        """
        try:
            cached = store().cached_categories()
            if cached is None:
                cached = await client().get_categories()
                store().cache_categories(cached)
            flat = _flatten(cached.get("categories") or [])
            if params.query:
                q = params.query.lower()
                nodes = [n for n in flat if q in n.name.lower()][:60]
                scope = "coincidencias"
            else:
                nodes = [n for n in flat if n.parent_id is None]
                scope = "raíces"
            summary = f"# Categorías ({scope}, {len(nodes)} de {len(flat)} en el árbol)\n\n" + "\n".join(
                f"- {n.name} (id {n.id}"
                + (f", padre {n.parent_id}" if n.parent_id else "")
                + (f", hijas {n.child_count}" if n.child_count else "")
                + ")"
                for n in nodes[:40]
            )
            return CategoriesResponse(summary=summary, count=len(nodes), categories=nodes)
        except Exception as exc:
            fail(exc)

    @mcp.tool(name="wp_get_seller", title="Perfil de vendedor", annotations=_READ)
    async def wp_get_seller(params: SellerInput) -> SellerResponse:
        """Lee el perfil público y las estadísticas de un vendedor.

        GET /users/{id} no trae la nota; GET /users/{id}/stats sí: rating_average,
        ventas (sold/sells), reseñas y anuncios publicados. No incluye el género.

        Returns:
            SellerResponse.
        """
        try:
            user = await client().get_user(params.user_id)
            stats = await client().get_user_stats(params.user_id)
            counters = {row.get("type"): row.get("value") for row in stats.get("counters") or [] if isinstance(row, dict)}
            ratings = {row.get("type"): row.get("value") for row in stats.get("ratings") or [] if isinstance(row, dict)}
            seller_type = user.get("seller_type") if isinstance(user.get("seller_type"), dict) else {}
            loc = user.get("location") if isinstance(user.get("location"), dict) else {}
            slug = user.get("web_slug")
            summary = (
                f"# {user.get('micro_name') or params.user_id}\n\n"
                f"- Tipo: {user.get('type')} · verificado: {seller_type.get('verified')}\n"
                f"- Nota media: {stats.get('rating_average')} · reseñas: {counters.get('reviews')}\n"
                f"- Publicados: {counters.get('publish')} · vendidos: {counters.get('sold')}\n"
                f"- Ciudad: {loc.get('city') or 'n/d'}"
            )
            return SellerResponse(
                summary=summary,
                user_id=params.user_id,
                micro_name=user.get("micro_name"),
                city=loc.get("city"),
                type=user.get("type"),
                verified=seller_type.get("verified"),
                register_date=epoch_to_iso(user.get("register_date")),
                rating_average=stats.get("rating_average"),
                reviews_score=ratings.get("reviews"),
                published=counters.get("publish"),
                sold=counters.get("sold"),
                sells=counters.get("sells"),
                reviews=counters.get("reviews"),
                web_slug=slug,
                profile_url=f"https://es.wallapop.com/user/{slug}" if slug else user.get("url_share"),
            )
        except Exception as exc:
            fail(exc)

    @mcp.tool(name="wp_get_seller_items", title="Anuncios de un vendedor", annotations=_READ)
    async def wp_get_seller_items(params: SellerItemsInput) -> SellerItemsResponse:
        """Lista los anuncios publicados visibles en el perfil público del vendedor.

        El endpoint no separa vendidos. El histórico de ventas está en wp_get_seller
        (contador sold) y en las reseñas, no como fichas recomprables.

        Returns:
            SellerItemsResponse.
        """
        try:
            rows = await client().get_user_items(params.user_id)
            cards = [apply_engagement(to_card(row)) for row in rows[: params.limit]]
            summary = f"# Anuncios de {params.user_id}\n\n" + bullets(cards)
            return SellerItemsResponse(
                summary=summary,
                user_id=params.user_id,
                count=len(cards),
                items=cards,
                note="Solo publicados visibles. Los vendidos no vienen en /users/{id}/items.",
            )
        except Exception as exc:
            fail(exc)


def _flatten(nodes: list, parent: int | None = None) -> list[CategoryNode]:
    out: list[CategoryNode] = []
    for node in nodes:
        if not isinstance(node, dict) or "id" not in node:
            continue
        children = node.get("subcategories") or []
        out.append(
            CategoryNode(
                id=int(node["id"]),
                name=str(node.get("name") or ""),
                parent_id=int(node["parent_id"]) if node.get("parent_id") else parent,
                icon=node.get("icon"),
                child_count=len(children) if isinstance(children, list) else 0,
            )
        )
        if isinstance(children, list):
            out.extend(_flatten(children, int(node["id"])))
    return out
