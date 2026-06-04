import os
from pathlib import Path


class Config:
    BASE_DIR = Path(__file__).resolve().parent.parent
    CACHE_DIR = BASE_DIR / "data" / "cache"
    CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", str(60 * 60 * 6)))
    DEFAULT_ZIP_CODE = os.getenv("DEFAULT_ZIP_CODE", "14195")
    MAX_OFFERS_PER_RETAILER = int(os.getenv("MAX_OFFERS_PER_RETAILER", "60"))
    REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "15"))
    REQUEST_VERIFY_TLS = os.getenv("REQUEST_VERIFY_TLS", "auto")

    RETAILERS = {
        "aldi-nord": "ALDI Nord",
        "edeka": "EDEKA",
        "rewe": "REWE",
        "lidl": "Lidl",
        "netto-marken-discount": "Netto Marken-Discount",
        "kaufland": "Kaufland",
    }

    DRUGSTORE_RETAILERS = {
        "dm-drogerie-markt": "dm",
        "rossmann": "ROSSMANN",
    }

    CATEGORY_LABELS = {
        "produce": {"de": "Obst & Gemüse", "ko": "과일/채소"},
        "dairy": {"de": "Molkerei", "ko": "유제품"},
        "meat_fish": {"de": "Fleisch & Fisch", "ko": "육류/생선"},
        "bakery": {"de": "Bäckerei", "ko": "빵/베이커리"},
        "pantry": {"de": "Vorrat", "ko": "식료품"},
        "drinks": {"de": "Getränke", "ko": "음료"},
        "household": {"de": "Haushalt", "ko": "생활용품"},
        "personal_care": {"de": "Körperpflege", "ko": "개인위생"},
        "baby": {"de": "Baby", "ko": "유아용품"},
        "other": {"de": "Sonstiges", "ko": "기타"},
    }
