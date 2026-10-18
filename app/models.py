from dataclasses import asdict, dataclass


@dataclass(slots=True)
class Offer:
    id: str
    source_offer_id: int
    retailer: str
    retailer_slug: str
    title: str
    brand: str | None
    category: str
    price: float | None
    old_price: float | None
    unit_price: float | None
    unit: str | None
    discount_percent: float | None
    description: str
    valid_from: str | None
    valid_to: str | None
    image_url: str | None
    source_url: str

    product_key: str | None = None
    all_time_low: float | None = None
    is_all_time_low: bool = False
    recorded_at: str | None = None
    competitors: list[dict] | None = None
    cheapest_competitor: dict | None = None
    is_cheapest_in_week: bool = False

    # Regional Scope & Provenance
    requested_zip_code: str | None = None
    scope_type: str = "unknown"  # "store", "region", "nationwide", "unknown"
    store_id: str | None = None
    store_name: str | None = None
    store_address: str | None = None
    source_provider: str | None = None  # e.g., "edeka-official", "marktguru"
    source_region_id: str | None = None
    data_updated_at: str | None = None
    region_verified: bool = False
    regional_all_time_low: float | None = None
    is_regional_all_time_low: bool = False
    national_all_time_low: float | None = None
    is_national_all_time_low: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Offer":
        filtered = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**filtered)


@dataclass(slots=True)
class OfferResult:
    offers: list[Offer]
    fetched_at: str | None
    warnings: list[str]
    from_cache: bool = False
