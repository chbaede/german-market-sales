from abc import ABC, abstractmethod

from app.models import Offer


class OfferProvider(ABC):
    @abstractmethod
    def fetch_offers(self, retailer_slug: str, zip_code: str, limit: int) -> tuple[list[Offer], list[str]]:
        raise NotImplementedError
