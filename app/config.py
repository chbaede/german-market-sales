import os
from pathlib import Path

WEEK_LABELS = {
    "current": {"de": "Diese Woche", "ko": "이번 주", "en": "This week"},
    "next": {"de": "Nächste Woche", "ko": "다음 주", "en": "Next week"},
}

SUPERMARKET_PAGE = {
    "title": "Supermarkt-Angebote · 독일 마트 할인",
    "meta_description": (
        "Wöchentliche Supermarkt-Angebote in Deutschland (ALDI, LIDL, REWE, EDEKA, Kaufland, Netto, PENNY) "
        "vergleichen und sparen. Aktuelle Prospekte, Rabatte und günstige Lebensmittel."
    ),
    "eyebrow": "ALDI · EDEKA · REWE · Lidl · Netto · PENNY · nahkauf · Kaufland",
    "heading": "Wöchentliche Angebote",
    "heading_ko": "이번 주 할인 생필품",
    "heading_en": "Weekly Essentials Deals",
    "active": "supermarket",
    "refresh_endpoint": "main.refresh",
    "basket_endpoint": "main.api_basket",
    "storage_key": "supermarketDealShoppingList",
    "retailer_label": {"de": "Händler", "ko": "마트", "en": "Retailer"},
    "search_placeholder": "butter, kaffee, pasta",
    "basket_placeholder": "파 4개, 당근, 우유 2개",
    "basket_placeholder_en": "4 green onions, carrots, 2 milk",
    "week_labels": WEEK_LABELS,
    "default_week": "current",
    "default_retailers": ("aldi-nord", "edeka", "rewe", "lidl"),
    "basket_templates": (
        {
            "label": "Basis",
            "ko": "기본",
            "en": "Basic",
            "value": "우유 2개, 계란, 양파, 당근, 토마토, 바나나, 빵, 버터",
            "value_en": "2 milk, eggs, onions, carrots, tomatoes, bananas, bread, butter",
        },
        {
            "label": "Kochen",
            "ko": "요리",
            "en": "Cooking",
            "value": "파 4개, 감자 1kg, 파스타, 쌀, 닭고기, 치즈",
            "value_en": "4 green onions, 1kg potatoes, pasta, rice, chicken, cheese",
        },
        {
            "label": "Haushalt",
            "ko": "생활",
            "en": "Household",
            "value": "세제, 주방타월, 화장지, 쓰레기봉투",
            "value_en": "detergent, paper towels, toilet paper, trash bags",
        },
    ),
}

DRUGSTORE_PAGE = {
    "title": "Drogerie-Angebote · DM/ROSSMANN 할인",
    "meta_description": (
        "Aktuelle Drogerie-Angebote bei dm, ROSSMANN und budni vergleichen. "
        "Entdecken Sie Rabatte auf Kosmetik, Pflegeprodukte, Haushaltswaren und Babyartikel."
    ),
    "eyebrow": "dm · ROSSMANN · budni",
    "heading": "Drogerie-Angebote",
    "heading_ko": "DM/ROSSMANN 할인 생활용품",
    "heading_en": "DM/ROSSMANN Deals",
    "active": "drugstore",
    "refresh_endpoint": "main.drugstores_refresh",
    "basket_endpoint": "main.api_drugstore_basket",
    "storage_key": "drugstoreDealShoppingList",
    "retailer_label": {"de": "Drogerie", "ko": "드럭스토어", "en": "Drugstore"},
    "search_placeholder": "shampoo, zahnpasta, waschmittel",
    "basket_placeholder": "샴푸 1개, 치약, 세제, 기저귀",
    "basket_placeholder_en": "1 shampoo, toothpaste, detergent, diapers",
    "week_labels": WEEK_LABELS,
    "default_week": "current",
    "default_retailers": ("dm-drogerie-markt", "rossmann"),
    "basket_templates": (
        {
            "label": "Basis",
            "ko": "기본",
            "en": "Basic",
            "value": "샴푸 1개, 치약, 바디워시, 비누, 데오",
            "value_en": "1 shampoo, toothpaste, body wash, soap, deodorant",
        },
        {
            "label": "Haushalt",
            "ko": "생활",
            "en": "Household",
            "value": "세제, 화장지, 주방세제, 청소포",
            "value_en": "detergent, toilet paper, dish soap, cleaning wipes",
        },
        {
            "label": "Baby",
            "ko": "유아",
            "en": "Baby",
            "value": "기저귀, 물티슈, 베이비크림",
            "value_en": "diapers, baby wipes, baby cream",
        },
    ),
}


class Config:
    BASE_DIR = Path(__file__).resolve().parent.parent
    CACHE_DIR = BASE_DIR / "data" / "cache"
    CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", str(60 * 60 * 6)))
    DEFAULT_ZIP_CODE = os.getenv("DEFAULT_ZIP_CODE", "14195")
    MAX_OFFERS_PER_RETAILER = int(os.getenv("MAX_OFFERS_PER_RETAILER", "60"))
    REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "15"))
    REQUEST_VERIFY_TLS = os.getenv("REQUEST_VERIFY_TLS", "auto")
    AUTO_REFRESH_ENABLED = os.getenv("AUTO_REFRESH_ENABLED", "true").lower() == "true"
    SCHEDULER_CHECK_INTERVAL_SECONDS = int(os.getenv("SCHEDULER_CHECK_INTERVAL_SECONDS", "900"))
    SCHEDULER_MAX_CACHE_AGE_HOURS = int(os.getenv("SCHEDULER_MAX_CACHE_AGE_HOURS", "24"))

    RETAILERS = {
        "aldi-nord": "ALDI Nord",
        "edeka": "EDEKA",
        "rewe": "REWE",
        "lidl": "Lidl",
        "netto-marken-discount": "Netto Marken-Discount",
        "penny": "PENNY",
        "nahkauf": "nahkauf",
        "kaufland": "Kaufland",
    }

    DRUGSTORE_RETAILERS = {
        "dm-drogerie-markt": "dm",
        "rossmann": "ROSSMANN",
        "budnikowsky": "budni",
    }

    CATEGORY_LABELS = {
        "produce": {"de": "Obst & Gemüse", "ko": "과일/채소"},
        "dairy": {"de": "Molkerei", "ko": "유제품"},
        "meat_fish": {"de": "Fleisch & Fisch", "ko": "육류/생선"},
        "bakery": {"de": "Bäckerei", "ko": "빵/베이커리"},
        "pantry": {"de": "Vorrat", "ko": "식료품"},
        "drinks": {"de": "Getränke", "ko": "음료"},
        "household": {"de": "Haushalt", "ko": "생활용품", "en": "Household"},
        "personal_care": {"de": "Körperpflege", "ko": "개인위생"},
        "baby": {"de": "Baby", "ko": "유아용품"},
        "nonfood": {"de": "Nonfood & Kleidung", "ko": "비식품/의류"},
        "other": {"de": "Sonstiges", "ko": "기타", "en": "Other"},
    }
