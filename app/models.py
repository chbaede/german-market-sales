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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Offer":
        return cls(**data)


@dataclass(slots=True)
class OfferResult:
    offers: list[Offer]
    fetched_at: str | None
    warnings: list[str]
    from_cache: bool = False
