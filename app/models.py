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
