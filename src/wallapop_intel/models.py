"""Modelos Pydantic de entrada y salida."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class OrderBy(str, Enum):
    newest = "newest"
    price_low_to_high = "price_low_to_high"
    price_high_to_low = "price_high_to_low"
    most_relevance = "most_relevance"
    closest = "closest"


class Timeframe(str, Enum):
    any = "any"
    today = "today"
    last_week = "lastWeek"
    last_month = "lastMonth"


class RankMetric(str, Enum):
    views = "views"
    favorites = "favorites"
    engagement = "engagement"


class ResponseFormat(str, Enum):
    markdown = "markdown"
    json = "json"


class SearchInput(_Strict):
    keywords: str = Field(..., description="Texto de búsqueda, p. ej. 'iphone 13'", min_length=2, max_length=120)
    min_price: float | None = Field(default=None, description="Precio mínimo en EUR (ej. 50)", ge=0, le=1_000_000)
    max_price: float | None = Field(default=None, description="Precio máximo en EUR (ej. 400)", ge=0, le=1_000_000)
    category_id: int | None = Field(default=None, description="Id de categoría, p. ej. 24200", ge=1)
    latitude: float | None = Field(default=None, description="Latitud WGS84. Por defecto WALLAPOP_LATITUDE", ge=-90, le=90)
    longitude: float | None = Field(default=None, description="Longitud WGS84. Por defecto WALLAPOP_LONGITUDE", ge=-180, le=180)
    distance_km: int | None = Field(default=None, description="Radio en km (ej. 50). Se envía como distance_in_km", ge=1, le=500)
    order_by: OrderBy = Field(default=OrderBy.newest, description="newest | price_low_to_high | price_high_to_low | most_relevance | closest")
    timeframe: Timeframe = Field(default=Timeframe.any, description="Filtro local por created_at: any | today | lastWeek | lastMonth")
    limit: int = Field(default=20, description="Máximo de anuncios a devolver", ge=1, le=40)
    cursor: str | None = Field(default=None, description="Cursor meta.next_page de la página anterior")
    include_details: bool = Field(default=False, description="Si true, pide la ficha (counters) de cada anuncio. Máx. 8 por llamada")
    response_format: ResponseFormat = Field(default=ResponseFormat.markdown)


class ItemIdInput(_Strict):
    item_id: str = Field(..., description="Id Wallapop, p. ej. 'w67v2xx3896x'", min_length=4, max_length=32)
    response_format: ResponseFormat = Field(default=ResponseFormat.markdown)


class CategoriesInput(_Strict):
    query: str | None = Field(default=None, description="Filtro por nombre, p. ej. 'consola'", max_length=80)
    response_format: ResponseFormat = Field(default=ResponseFormat.markdown)


class SellerInput(_Strict):
    user_id: str = Field(..., description="Id de vendedor, p. ej. 'nzx5y8xd2562'", min_length=4, max_length=32)
    response_format: ResponseFormat = Field(default=ResponseFormat.markdown)


class SellerItemsInput(SellerInput):
    limit: int = Field(default=20, description="Máximo de anuncios del perfil público", ge=1, le=40)


class RankInput(SearchInput):
    metric: RankMetric = Field(default=RankMetric.engagement, description="views | favorites | engagement")
    min_reliability: str = Field(default="low", description="low | medium | high. Descarta métricas por debajo")
    detail_cap: int = Field(default=8, description="Cuántas fichas pedir para counters reales", ge=1, le=12)


class MarketInput(_Strict):
    keywords: str = Field(..., min_length=2, max_length=120, description="Producto o keyword, p. ej. 'ps5'")
    category_id: int | None = Field(default=None, ge=1)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    distance_km: int | None = Field(default=None, ge=1, le=500)
    limit: int = Field(default=30, ge=5, le=40)
    response_format: ResponseFormat = Field(default=ResponseFormat.markdown)


class PriceHistoryInput(_Strict):
    item_id: str | None = Field(default=None, description="Id de anuncio. Alternativa a keywords", max_length=32)
    keywords: str | None = Field(default=None, description="Producto vigilado en snapshots", max_length=120)
    limit: int = Field(default=30, ge=1, le=200)
    response_format: ResponseFormat = Field(default=ResponseFormat.markdown)


class OpportunityInput(MarketInput):
    max_results: int = Field(default=8, ge=1, le=20, description="Oportunidades a devolver")


class SearchLotsInput(MarketInput):
    max_results: int = Field(default=10, ge=1, le=20, description="Lotes/oportunidades a devolver")
    min_opportunity_score: float = Field(
        default=15.0,
        ge=0,
        le=100,
        description="Filtra anuncios con score compuesto por debajo de este umbral",
    )
    append_lot_keywords: bool = Field(
        default=True,
        description="Si true, amplía la búsqueda con términos como lote, pack, para piezas",
    )


class WeightBand(str, Enum):
    under_2kg = "under_2kg"
    kg_2_5 = "kg_2_5"
    kg_5_10 = "kg_5_10"
    kg_10_20 = "kg_10_20"
    over_20kg = "over_20kg"


class EstimateProfitInput(_Strict):
    purchase_price: float = Field(..., description="Precio de compra en EUR", ge=0, le=1_000_000)
    expected_resale_price: float = Field(..., description="Precio de venta esperado en EUR", ge=0, le=1_000_000)
    weight_kg: float | None = Field(
        default=None,
        description="Peso estimado del paquete en kg. Alternativa a weight_band",
        ge=0.01,
        le=500,
    )
    weight_band: WeightBand | None = Field(
        default=None,
        description="Tramo Wallapop Envíos si no conoces el peso exacto",
    )
    include_outbound_shipping: bool = Field(
        default=True,
        description="Resta el envío al vender (tú envías con Wallapop Envíos)",
    )
    include_inbound_shipping: bool = Field(
        default=False,
        description="Resta un envío equivalente al comprar con envío",
    )
    include_buyer_protection: bool = Field(
        default=True,
        description="Resta la protección comprador (~2.50€ + 5% del precio de venta) como coste conservador",
    )
    other_costs: float = Field(default=0.0, description="Gastos extra (embalaje, comisiones, etc.) en EUR", ge=0, le=100_000)
    conservative_shipping: bool = Field(
        default=True,
        description="Si true, usa el extremo alto del tramo de envío cuando hay rango",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.markdown)


class WatchAddInput(_Strict):
    kind: str = Field(..., description="'item' o 'keyword'", pattern="^(item|keyword)$")
    key: str = Field(..., description="item_id o texto de búsqueda", min_length=2, max_length=120)
    label: str | None = Field(default=None, max_length=160)


class WatchRemoveInput(_Strict):
    kind: str = Field(..., pattern="^(item|keyword)$")
    key: str = Field(..., min_length=2, max_length=120)


class WatchSnapshotInput(_Strict):
    response_format: ResponseFormat = Field(default=ResponseFormat.markdown)


class MyItemsInput(_Strict):
    limit: int = Field(default=10, ge=1, le=20)
    response_format: ResponseFormat = Field(default=ResponseFormat.markdown)


class MyAdviceInput(_Strict):
    item_id: str = Field(..., min_length=4, max_length=32, description="Id de uno de tus anuncios")
    response_format: ResponseFormat = Field(default=ResponseFormat.markdown)


class ItemCard(BaseModel):
    id: str
    title: str
    price_eur: float | None = None
    currency: str | None = None
    city: str | None = None
    user_id: str | None = None
    category_id: str | None = None
    web_slug: str | None = None
    url: str | None = None
    created_at: str | None = None
    reserved: bool | None = None
    shippable: bool | None = None
    image_url: str | None = None
    views: int | None = None
    favorites: int | None = None
    conversations: int | None = None
    metrics_method: str | None = None
    reliability: str | None = None
    engagement_score: float | None = None


class SearchResponse(BaseModel):
    summary: str
    keywords: str
    count: int
    has_more: bool
    next_cursor: str | None = None
    items: list[ItemCard]
    note: str


class ItemDetailResponse(BaseModel):
    summary: str
    item: ItemCard
    description: str | None = None
    condition: str | None = None
    characteristics: str | None = None
    in_demand_visible: bool | None = None
    counters_are_real: bool
    raw_field_names: list[str]


class CategoryNode(BaseModel):
    id: int
    name: str
    parent_id: int | None = None
    icon: str | None = None
    child_count: int = 0


class CategoriesResponse(BaseModel):
    summary: str
    count: int
    categories: list[CategoryNode]


class SellerResponse(BaseModel):
    summary: str
    user_id: str
    micro_name: str | None = None
    city: str | None = None
    type: str | None = None
    verified: bool | None = None
    register_date: str | None = None
    rating_average: float | None = None
    reviews_score: float | None = None
    published: int | None = None
    sold: int | None = None
    sells: int | None = None
    reviews: int | None = None
    web_slug: str | None = None
    profile_url: str | None = None


class SellerItemsResponse(BaseModel):
    summary: str
    user_id: str
    count: int
    items: list[ItemCard]
    note: str


class MetricsResponse(BaseModel):
    summary: str
    item_id: str
    title: str | None = None
    views: int | None = None
    favorites: int | None = None
    conversations: int | None = None
    views_source: str
    favorites_source: str
    engagement_score: float
    reliability: str
    method: str
    explanation: str


class RankResponse(BaseModel):
    summary: str
    metric: str
    count: int
    items: list[ItemCard]
    explanation: str


class MarketResponse(BaseModel):
    summary: str
    keywords: str
    sample_size: int
    min_price: float | None = None
    median_price: float | None = None
    max_price: float | None = None
    p25: float | None = None
    p75: float | None = None
    newest_per_day_estimate: float | None = None
    price_drops: list[dict[str, Any]]
    disappeared: list[dict[str, Any]]
    note: str


class PriceHistoryResponse(BaseModel):
    summary: str
    points: list[dict[str, Any]]
    note: str


class Opportunity(BaseModel):
    item: ItemCard
    score: float
    median_price: float | None = None
    discount_ratio: float | None = None
    why: str


class OpportunitiesResponse(BaseModel):
    summary: str
    median_price: float | None = None
    opportunities: list[Opportunity]
    note: str


class LotOpportunity(BaseModel):
    item: ItemCard
    opportunity_score: float
    signal_score: float
    price_score: float
    urgency_score: float
    signals: list[str]
    median_price: float | None = None
    discount_ratio: float | None = None
    why: str


class SearchLotsResponse(BaseModel):
    summary: str
    keywords: str
    search_query: str
    sample_size: int
    median_price: float | None = None
    lots: list[LotOpportunity]
    note: str


class ProfitEstimateResponse(BaseModel):
    summary: str
    purchase_price: float
    expected_resale_price: float
    gross_margin: float
    net_profit: float
    roi_percent: float | None
    outbound_shipping_eur: float
    inbound_shipping_eur: float
    buyer_protection_eur: float
    other_costs: float
    total_costs: float
    weight_kg_used: float
    weight_band: str | None
    assumptions: list[str]
    note: str


class Winner(BaseModel):
    label: str
    sample_size: int
    median_price: float | None = None
    mean_favorites: float | None = None
    mean_views: float | None = None
    score: float
    why: str


class WinnersResponse(BaseModel):
    summary: str
    winners: list[Winner]
    method: str
    note: str


class WatchResponse(BaseModel):
    summary: str
    entries: list[dict[str, Any]]


class AdviceResponse(BaseModel):
    summary: str
    item_id: str
    your_price: float | None = None
    market_median: float | None = None
    age_days: float | None = None
    views: int | None = None
    favorites: int | None = None
    photo_count: int | None = None
    recommendations: list[str]
    competitor_count: int
