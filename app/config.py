import os
from pathlib import Path

WEEK_LABELS = {
    "current": {"de": "Diese Woche", "ko": "이번 주", "en": "This week"},
    "next": {"de": "Nächste Woche", "ko": "다음 주", "en": "Next week"},
}

SUPERMARKET_PAGE = {
    "title": "Supermarkt-Angebote · 독일 마트 할인",
    "title_en": "Supermarkt-Angebote · German Supermarket Deals",
    "meta_description": (
        "Wöchentliche Supermarkt-Angebote in Deutschland (EDEKA, REWE, Lidl, ALDI Nord, ALDI Süd, Kaufland, Netto, PENNY) "
        "vergleichen und sparen. Aktuelle Prospekte, Rabatte und günstige Lebensmittel."
    ),
    "meta_description_en": (
        "Compare weekly supermarket deals in Germany (EDEKA, REWE, Lidl, ALDI Nord, ALDI Süd, Kaufland, Netto, PENNY) "
        "and save on groceries with current flyers and discounts."
    ),
    "eyebrow": "EDEKA · REWE · Lidl · ALDI Nord · ALDI Süd · Kaufland · Netto · PENNY · nahkauf",
    "heading": "Supermarkt Deals",
    "heading_ko": "독일 마트 주간 할인",
    "heading_en": "Weekly Supermarket Deals",
    "active": "supermarket",
    "refresh_endpoint": "main.refresh",
    "basket_endpoint": "main.api_basket",
    "storage_key": "supermarketDealShoppingList",
    "retailer_label": {"de": "Händler", "ko": "마트순", "en": "Retailer"},
    "search_placeholder": "상품 검색 (예: Butter, Kaffee, 우유)",
    "search_placeholder_en": "Search products (e.g. Butter, Kaffee, Milk)",
    "basket_placeholder": "파 4개, 당근, 우유 2개",
    "basket_placeholder_en": "4 green onions, carrots, 2 milk",
    "week_labels": WEEK_LABELS,
    "default_week": "current",
    "default_retailers": ("edeka",),
    "basket_templates": (),
}

DRUGSTORE_PAGE = {
    "title": "Drogerie-Angebote · DM/ROSSMANN 할인",
    "title_en": "Drogerie-Angebote · DM/ROSSMANN Deals",
    "meta_description": (
        "Aktuelle Drogerie-Angebote bei dm, ROSSMANN und budni vergleichen. "
        "Entdecken Sie Rabatte auf Kosmetik, Pflegeprodukte, Haushaltswaren und Babyartikel."
    ),
    "meta_description_en": (
        "Compare current drugstore deals at dm, ROSSMANN, and budni in Germany. "
        "Find discounts on cosmetics, personal care, household, and baby products."
    ),
    "eyebrow": "dm · ROSSMANN · budni",
    "heading": "Drogerie Deals",
    "heading_ko": "드러그스토어 주간 할인",
    "heading_en": "Weekly Drugstore Deals",
    "active": "drugstore",
    "refresh_endpoint": "main.drugstores_refresh",
    "basket_endpoint": "main.api_drugstore_basket",
    "storage_key": "drugstoreDealShoppingList",
    "retailer_label": {"de": "Drogerie", "ko": "매장순", "en": "Store"},
    "search_placeholder": "상품 검색 (예: Shampoo, Zahnpasta, 샴푸)",
    "search_placeholder_en": "Search products (e.g. Shampoo, Zahnpasta, Soap)",
    "basket_placeholder": "샴푸 1개, 치약, 세제, 기저귀",
    "basket_placeholder_en": "1 shampoo, toothpaste, detergent, diapers",
    "week_labels": WEEK_LABELS,
    "default_week": "current",
    "default_retailers": ("dm-drogerie-markt", "rossmann"),
    "basket_templates": (),
}


class Config:
    BASE_DIR = Path(__file__).resolve().parent.parent
    CACHE_DIR = BASE_DIR / "data" / "cache"
    CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", str(60 * 60 * 24 * 7)))
    DEFAULT_ZIP_CODE = os.getenv("DEFAULT_ZIP_CODE", "14195")
    MAX_OFFERS_PER_RETAILER = int(os.getenv("MAX_OFFERS_PER_RETAILER", "60"))
    REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "15"))
    REQUEST_VERIFY_TLS = os.getenv("REQUEST_VERIFY_TLS", "auto")
    AUTO_REFRESH_ENABLED = os.getenv("AUTO_REFRESH_ENABLED", "true").lower() == "true"
    SCHEDULER_CHECK_INTERVAL_SECONDS = int(os.getenv("SCHEDULER_CHECK_INTERVAL_SECONDS", "900"))
    SCHEDULER_MAX_CACHE_AGE_HOURS = int(os.getenv("SCHEDULER_MAX_CACHE_AGE_HOURS", "168"))

    RETAILERS = {
        "edeka": "EDEKA",
        "rewe": "REWE",
        "lidl": "Lidl",
        "aldi-nord": "ALDI Nord",
        "aldi-sued": "ALDI Süd",
        "kaufland": "Kaufland",
        "netto-marken-discount": "Netto",
        "penny": "PENNY",
        "nahkauf": "nahkauf",
    }

    DRUGSTORE_RETAILERS = {
        "dm-drogerie-markt": "dm",
        "rossmann": "ROSSMANN",
        "budnikowsky": "budni",
    }

    CATEGORY_LABELS = {
        "produce": {"de": "Obst & Gemüse", "ko": "과일/채소", "en": "Produce"},
        "dairy": {"de": "Molkerei", "ko": "유제품", "en": "Dairy"},
        "meat_fish": {"de": "Fleisch & Fisch", "ko": "육류/생선", "en": "Meat & Fish"},
        "bakery": {"de": "Bäckerei", "ko": "빵/베이커리", "en": "Bakery"},
        "pantry": {"de": "Vorrat", "ko": "식료품", "en": "Pantry"},
        "drinks": {"de": "Getränke", "ko": "음료", "en": "Drinks"},
        "household": {"de": "Haushalt", "ko": "생활용품", "en": "Household"},
        "personal_care": {"de": "Körperpflege", "ko": "개인위생", "en": "Personal Care"},
        "baby": {"de": "Baby", "ko": "유아용품", "en": "Baby"},
        "nonfood": {"de": "Nonfood & Kleidung", "ko": "비식품/의류", "en": "Non-food"},
        "other": {"de": "Sonstiges", "ko": "기타", "en": "Other"},
    }
