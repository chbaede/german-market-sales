from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Lock
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for

from app.models import Offer
from app.services.basket import BasketRecommendation, recommend_basket_items
from app.services.offers import OfferService
from app.services.translations import translate_text

bp = Blueprint("main", __name__)
LOGGER = logging.getLogger(__name__)
BERLIN_TZ = ZoneInfo("Europe/Berlin")
BACKGROUND_REFRESH_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="offer-refresh")
BACKGROUND_REFRESH_LOCK = Lock()
BACKGROUND_REFRESH_KEYS: set[tuple[str, str]] = set()
WEEK_LABELS = {
    "current": {"de": "Diese Woche", "ko": "이번 주", "en": "This week"},
    "next": {"de": "Nächste Woche", "ko": "다음 주", "en": "Next week"},
}
ESSENTIAL_CATEGORY_PRIORITY = {
    "produce": 0,
    "dairy": 0,
    "meat_fish": 0,
    "bakery": 0,
    "pantry": 0,
    "drinks": 1,
    "household": 2,
    "personal_care": 2,
    "baby": 2,
    "nonfood": 5,
    "other": 6,
}
LOW_PRIORITY_RECOMMENDATION_CATEGORIES = {"nonfood", "other"}
RECOMMENDATION_CATEGORIES = frozenset(
    category for category in ESSENTIAL_CATEGORY_PRIORITY if category not in LOW_PRIORITY_RECOMMENDATION_CATEGORIES
)
PRICE_SORT_FALLBACK = 999999
SUPERMARKET_PAGE = {
    "title": "Supermarkt-Angebote · 독일 마트 할인",
    "eyebrow": "ALDI · EDEKA · REWE · Lidl · Netto · PENNY · nahkauf · Kaufland",
    "heading": "Wöchentliche Angebote",
    "heading_ko": "이번 주 할인 생필품", "heading_en": "Weekly Essentials Deals",
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
        {"label": "Basis", "ko": "기본", "en": "Basic", "value": "우유 2개, 계란, 양파, 당근, 토마토, 바나나, 빵, 버터", "value_en": "2 milk, eggs, onions, carrots, tomatoes, bananas, bread, butter"},
        {"label": "Kochen", "ko": "요리", "en": "Cooking", "value": "파 4개, 감자 1kg, 파스타, 쌀, 닭고기, 치즈", "value_en": "4 green onions, 1kg potatoes, pasta, rice, chicken, cheese"},
        {"label": "Haushalt", "ko": "생활", "en": "Household", "value": "세제, 주방타월, 화장지, 쓰레기봉투", "value_en": "detergent, paper towels, toilet paper, trash bags"},
    ),
}
DRUGSTORE_PAGE = {
    "title": "Drogerie-Angebote · DM/ROSSMANN 할인",
    "eyebrow": "dm · ROSSMANN · budni",
    "heading": "Drogerie-Angebote",
    "heading_ko": "DM/ROSSMANN 할인 생활용품", "heading_en": "DM/ROSSMANN Deals",
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
        {"label": "Basis", "ko": "기본", "en": "Basic", "value": "샴푸 1개, 치약, 바디워시, 비누, 데오", "value_en": "1 shampoo, toothpaste, body wash, soap, deodorant"},
        {"label": "Haushalt", "ko": "생활", "en": "Household", "value": "세제, 화장지, 주방세제, 청소포", "value_en": "detergent, toilet paper, dish soap, cleaning wipes"},
        {"label": "Baby", "ko": "유아", "en": "Baby", "value": "기저귀, 물티슈, 베이비크림", "value_en": "diapers, baby wipes, baby cream"},
    ),
}


def offer_service() -> OfferService:
    return OfferService(
        cache_dir=current_app.config["CACHE_DIR"],
        retailers=current_app.config["RETAILERS"],
        cache_ttl_seconds=current_app.config["CACHE_TTL_SECONDS"],
        timeout=current_app.config["REQUEST_TIMEOUT_SECONDS"],
        verify_tls=current_app.config["REQUEST_VERIFY_TLS"],
        max_offers_per_retailer=current_app.config["MAX_OFFERS_PER_RETAILER"],
    )


def drugstore_offer_service() -> OfferService:
    return OfferService(
        cache_dir=current_app.config["CACHE_DIR"],
        retailers=current_app.config["DRUGSTORE_RETAILERS"],
        cache_ttl_seconds=current_app.config["CACHE_TTL_SECONDS"],
        timeout=current_app.config["REQUEST_TIMEOUT_SECONDS"],
        verify_tls=current_app.config["REQUEST_VERIFY_TLS"],
        max_offers_per_retailer=current_app.config["MAX_OFFERS_PER_RETAILER"],
        cache_name="drugstore_offers.json",
    )


@bp.get("/")
def index():
    return render_offer_page(offer_service(), current_app.config["RETAILERS"], SUPERMARKET_PAGE, "main.index")


@bp.get("/drogerie")
def drugstores():
    return render_offer_page(
        drugstore_offer_service(),
        current_app.config["DRUGSTORE_RETAILERS"],
        DRUGSTORE_PAGE,
        "main.drugstores",
    )


def render_offer_page(
    service: OfferService,
    retailers: dict[str, str],
    page: dict,
    page_endpoint: str,
):
    lang = request.cookies.get("lang", "ko")
    zip_code = _clean_zip(request.args.get("zip_code")) or current_app.config["DEFAULT_ZIP_CODE"]
    week_labels = page.get("week_labels", WEEK_LABELS)
    selected_week = _clean_week(request.args.get("week"), week_labels, page.get("default_week", "current"))
    result = load_offers_for_request(service, zip_code)

    selected_retailers = _selected_retailers(retailers, page.get("default_retailers", ()))
    query = (request.args.get("q") or "").strip()
    basket_query = (request.args.get("basket") or "").strip()
    category = request.args.get("category") or "all"
    sort = request.args.get("sort") or "essentials"
    priced_only = request.args.get("priced") == "1"
    ending_soon = request.args.get("ending") == "1"

    week_offers = filter_offers_by_week(result.offers, selected_week)
    filtered = filter_offers(week_offers, selected_retailers, query, category, priced_only, ending_soon)
    filtered = sort_offers(filtered, sort)
    filtered = limit_offers_per_retailer(filtered, current_app.config["MAX_OFFERS_PER_RETAILER"])
    recommendations = pick_recommendations(filtered)

    return render_template(
        "offers.html",
        offers=filtered,
        recommendations=recommendations,
        basket_query=basket_query,
        stats=build_stats(filtered, week_offers),
        retailers=retailers,
        category_labels=current_app.config["CATEGORY_LABELS"],
        selected_retailers=selected_retailers,
        selected_week=selected_week,
        selected_week_label=week_labels[selected_week],
        week_counts=build_week_counts(
            result.offers,
            week_labels,
            selected_retailers,
            query,
            category,
            priced_only,
            ending_soon,
        ),
        retailer_week_status=build_retailer_week_status(result.offers, retailers, selected_week, selected_retailers),
        retailer_counts=build_retailer_counts(week_offers, retailers, query, category, priced_only, ending_soon),
        default_retailers=page.get("default_retailers", ()),
        week_labels=week_labels,
        week_links=build_week_links(page_endpoint, week_labels),
        selected_category=category,
        selected_sort=sort,
        priced_only=priced_only,
        ending_soon=ending_soon,
        query=query,
        zip_code=zip_code,
        fetched_at=result.fetched_at,
        warnings=result.warnings,
        from_cache=result.from_cache,
        page=page,
        lang=lang,
    )


@bp.get("/refresh")
def refresh():
    args = request.args.to_dict(flat=False)
    args["refresh"] = ["1"]
    return redirect(url_for("main.index", **args))


@bp.get("/drogerie/refresh")
def drugstores_refresh():
    args = request.args.to_dict(flat=False)
    args["refresh"] = ["1"]
    return redirect(url_for("main.drugstores", **args))


@bp.get("/api/offers")
def api_offers():
    return api_offers_for(offer_service(), WEEK_LABELS)


@bp.get("/api/drogerie/offers")
def api_drugstore_offers():
    return api_offers_for(drugstore_offer_service(), WEEK_LABELS)


def api_offers_for(service: OfferService, week_labels: dict[str, dict[str, str]], default_week: str = "current"):
    zip_code = _clean_zip(request.args.get("zip_code")) or current_app.config["DEFAULT_ZIP_CODE"]
    selected_week = _clean_week(request.args.get("week"), week_labels, default_week)
    result = load_offers_for_request(service, zip_code)
    offers = filter_offers_by_week(result.offers, selected_week)
    return jsonify(
        {
            "zip_code": zip_code,
            "week": selected_week,
            "fetched_at": result.fetched_at,
            "from_cache": result.from_cache,
            "warnings": result.warnings,
            "offers": [offer.to_dict() for offer in offers],
        }
    )


@bp.get("/api/basket")
def api_basket():
    return api_basket_for(offer_service(), WEEK_LABELS)


@bp.get("/api/drogerie/basket")
def api_drugstore_basket():
    return api_basket_for(drugstore_offer_service(), WEEK_LABELS)


def api_basket_for(service: OfferService, week_labels: dict[str, dict[str, str]], default_week: str = "current"):
    zip_code = _clean_zip(request.args.get("zip_code")) or current_app.config["DEFAULT_ZIP_CODE"]
    selected_week = _clean_week(request.args.get("week"), week_labels, default_week)
    selected_retailers = set(request.args.getlist("retailer"))
    basket_query = (request.args.get("basket") or "").strip()
    priced_only = request.args.get("priced") == "1"
    ending_soon = request.args.get("ending") == "1"
    result = load_offers_for_request(service, zip_code)
    offers = filter_offers_by_week(result.offers, selected_week)
    offers = filter_offers(
        offers,
        selected_retailers,
        query="",
        category="all",
        priced_only=priced_only,
        ending_soon=ending_soon,
    )
    recommendations = recommend_basket_items(basket_query, offers)
    return jsonify(
        {
            "zip_code": zip_code,
            "week": selected_week,
            "week_label": week_labels[selected_week],
            "retailers": sorted(selected_retailers),
            "query": basket_query,
            "recommendations": [_basket_recommendation_to_dict(item) for item in recommendations],
        }
    )


@bp.get("/health")
def health():
    return {"status": "ok"}


def filter_offers(
    offers: list[Offer],
    selected_retailers: set[str],
    query: str,
    category: str,
    priced_only: bool = False,
    ending_soon: bool = False,
) -> list[Offer]:
    query_key = query.casefold()
    filtered: list[Offer] = []
    for offer in offers:
        if selected_retailers and offer.retailer_slug not in selected_retailers:
            continue
        if category != "all" and offer.category != category:
            continue
        if priced_only and offer.price is None and offer.unit_price is None:
            continue
        if ending_soon and not _ends_soon(offer):
            continue
        if query_key:
            haystack = " ".join(
                value for value in (offer.title, offer.brand or "", offer.description, offer.retailer) if value
            ).casefold()
            if query_key not in haystack:
                continue
        filtered.append(offer)
    return filtered


def filter_offers_by_week(offers: list[Offer], week: str, today: date | None = None) -> list[Offer]:
    today = today or datetime.now(BERLIN_TZ).date()
    if week == "next":
        start = _next_week_start(today)
        end = start + timedelta(days=6)
        return [offer for offer in offers if _starts_in_range(offer, start, end)]
    return [offer for offer in offers if _is_active_on(offer, today)]


def sort_offers(offers: list[Offer], sort: str) -> list[Offer]:
    if sort == "essentials":
        return sorted(
            offers,
            key=lambda offer: (
                _essential_priority(offer),
                -_discount_sort_value(offer),
                _deal_price(offer),
                offer.title.casefold(),
            ),
        )
    if sort == "price":
        return sorted(offers, key=lambda offer: (offer.price is None, offer.price or 0, offer.title.casefold()))
    if sort == "unit":
        return sorted(offers, key=lambda offer: (offer.unit_price is None, offer.unit_price or 0, offer.title.casefold()))
    if sort == "retailer":
        return sorted(offers, key=lambda offer: (offer.retailer.casefold(), offer.title.casefold()))
    if sort == "valid_to":
        return sorted(offers, key=lambda offer: (offer.valid_to or "", offer.retailer.casefold()))
    return sorted(
        offers,
        key=lambda offer: (-_discount_sort_value(offer), _price_or_fallback(offer), offer.title.casefold()),
    )


def limit_offers_per_retailer(offers: list[Offer], limit: int) -> list[Offer]:
    counts: dict[str, int] = {}
    limited: list[Offer] = []
    for offer in offers:
        count = counts.get(offer.retailer_slug, 0)
        if count >= limit:
            continue
        counts[offer.retailer_slug] = count + 1
        limited.append(offer)
    return limited


def pick_recommendations(offers: list[Offer]) -> list[Offer]:
    ranked = [
        offer
        for offer in offers
        if offer.category in RECOMMENDATION_CATEGORIES and (_discount_sort_value(offer) >= 10 or offer.price is not None)
    ]
    return sorted(
        ranked,
        key=lambda offer: (_essential_priority(offer), -_discount_sort_value(offer), _deal_price(offer)),
    )[:8]


def _essential_priority(offer: Offer) -> int:
    return ESSENTIAL_CATEGORY_PRIORITY.get(offer.category, ESSENTIAL_CATEGORY_PRIORITY["other"])


def _discount_sort_value(offer: Offer) -> float:
    return offer.discount_percent or 0


def _deal_price(offer: Offer) -> float:
    if offer.unit_price is not None:
        return offer.unit_price
    return _price_or_fallback(offer)


def _price_or_fallback(offer: Offer) -> float:
    return offer.price if offer.price is not None else PRICE_SORT_FALLBACK


def build_stats(filtered: list[Offer], all_offers: list[Offer]) -> dict:
    discounted = [offer for offer in filtered if offer.discount_percent]
    best = max((offer.discount_percent or 0 for offer in filtered), default=0)
    return {
        "visible": len(filtered),
        "total": len(all_offers),
        "discounted": len(discounted),
        "best_discount": round(best),
        "ending_soon": sum(1 for offer in filtered if _ends_soon(offer)),
        "retailer_count": len({offer.retailer_slug for offer in filtered}),
    }


def build_week_counts(
    offers: list[Offer],
    week_labels: dict[str, dict[str, str]],
    selected_retailers: set[str] | None = None,
    query: str = "",
    category: str = "all",
    priced_only: bool = False,
    ending_soon: bool = False,
) -> dict[str, int]:
    return {
        week: len(
            filter_offers(
                filter_offers_by_week(offers, week),
                selected_retailers or set(),
                query,
                category,
                priced_only,
                ending_soon,
            )
        )
        for week in week_labels
    }


def build_retailer_counts(
    offers: list[Offer],
    retailers: dict[str, str],
    query: str = "",
    category: str = "all",
    priced_only: bool = False,
    ending_soon: bool = False,
) -> dict[str, int]:
    counts = {slug: 0 for slug in retailers}
    for offer in filter_offers(offers, set(), query, category, priced_only, ending_soon):
        if offer.retailer_slug in counts:
            counts[offer.retailer_slug] += 1
    return counts


def build_retailer_week_status(
    offers: list[Offer],
    retailers: dict[str, str],
    week: str,
    selected_retailers: set[str] | None = None,
) -> dict[str, list[dict[str, int | str]]]:
    target_slugs = [slug for slug in retailers if not selected_retailers or slug in selected_retailers]
    counts = {slug: 0 for slug in target_slugs}
    for offer in filter_offers_by_week(offers, week):
        if offer.retailer_slug in counts:
            counts[offer.retailer_slug] += 1

    return {
        "available": [
            {"slug": slug, "label": retailers[slug], "count": counts[slug]} for slug in target_slugs if counts[slug] > 0
        ],
        "missing": [
            {"slug": slug, "label": retailers[slug], "count": counts[slug]} for slug in target_slugs if counts[slug] == 0
        ],
    }


def _selected_retailers(retailers: dict[str, str], default_retailers: tuple[str, ...] | list[str]) -> set[str]:
    requested = request.args.getlist("retailer")
    if requested or request.args.get("retailer_filter") == "1":
        return {slug for slug in requested if slug in retailers}
    return {slug for slug in default_retailers if slug in retailers}


def load_offers_for_request(service: OfferService, zip_code: str) -> OfferResult:
    refresh_mode = request.args.get("refresh")
    if refresh_mode == "1":
        return service.get_offers(zip_code=zip_code, refresh=True)
    if refresh_mode == "0":
        return service.get_offers(zip_code=zip_code, refresh=False)

    cached_getter = getattr(service, "get_cached_offers", None)
    if cached_getter:
        cached = cached_getter(zip_code)
        if cached:
            _queue_refresh(service, zip_code)
            return cached
    return service.get_offers(zip_code=zip_code, refresh=True)


def _queue_refresh(service: OfferService, zip_code: str) -> None:
    key = _refresh_key(service, zip_code)
    with BACKGROUND_REFRESH_LOCK:
        if key in BACKGROUND_REFRESH_KEYS:
            return
        BACKGROUND_REFRESH_KEYS.add(key)
    BACKGROUND_REFRESH_EXECUTOR.submit(_refresh_cache, service, zip_code, key)


def _refresh_cache(service: OfferService, zip_code: str, key: tuple[str, str]) -> None:
    try:
        service.get_offers(zip_code=zip_code, refresh=True)
    except Exception:
        LOGGER.exception("Background offer refresh failed for zip_code=%s", zip_code)
    finally:
        with BACKGROUND_REFRESH_LOCK:
            BACKGROUND_REFRESH_KEYS.discard(key)


def _refresh_key(service: OfferService, zip_code: str) -> tuple[str, str]:
    cache_file = getattr(service, "cache_file", None)
    return (str(cache_file) if cache_file else str(id(service)), zip_code)


def build_week_links(endpoint: str, week_labels: dict[str, dict[str, str]]) -> dict[str, str]:
    links: dict[str, str] = {}
    for week in week_labels:
        args = request.args.to_dict(flat=False)
        args.pop("refresh", None)
        args["week"] = [week]
        links[week] = url_for(endpoint, **args)
    return links


def _basket_recommendation_to_dict(recommendation: BasketRecommendation) -> dict:
    return {
        "item": {
            "raw": recommendation.item.raw,
            "name": recommendation.item.name,
            "amount": recommendation.item.amount,
            "unit": recommendation.item.unit,
            "amount_text": _format_amount(recommendation.item.amount, recommendation.item.unit),
        },
        "candidates": [
            {
                "retailer": candidate.offer.retailer,
                "retailer_slug": candidate.offer.retailer_slug,
                "title": candidate.offer.title,
                "title_ko": translate_text(candidate.offer.title),
                "description": candidate.offer.description,
                "image_url": candidate.offer.image_url,
                "price_text": _format_eur(candidate.offer.price),
                "old_price_text": _format_eur(_old_price(candidate.offer)),
                "old_price_estimated": candidate.offer.old_price is None and _old_price(candidate.offer) is not None,
                "discount_text": _format_percent(_discount_percent(candidate.offer)),
                "unit_price_text": _format_unit_price(candidate.offer),
                "estimated_total_text": _format_eur(candidate.estimated_total),
                "estimate_note": candidate.estimate_note,
                "valid_text": f"{_format_date_de(candidate.offer.valid_from)} - {_format_date_de(candidate.offer.valid_to)}",
                "source_url": candidate.offer.source_url,
            }
            for candidate in recommendation.candidates
        ],
    }


def _clean_zip(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    return value if value.isdigit() and 4 <= len(value) <= 5 else None


def _format_eur(value: float | int | None) -> str | None:
    if value is None:
        return None
    return f"{value:,.2f} EUR".replace(",", "X").replace(".", ",").replace("X", ".")


def _format_percent(value: float | int | None) -> str | None:
    if value is None or value <= 0:
        return None
    return f"-{round(value)}%"


def _format_amount(value: float | None, unit: str | None) -> str:
    if value is None:
        return ""
    amount = str(int(value)) if float(value).is_integer() else str(value).replace(".", ",")
    return " ".join(part for part in (amount, unit or "") if part)


def _format_unit_price(offer: Offer) -> str | None:
    if offer.unit_price is None:
        return None
    return f"{_format_eur(offer.unit_price)} / {offer.unit or 'unit'}"


def _format_date_de(value: str | None) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        date_part = parsed.astimezone(BERLIN_TZ).date().isoformat()
    except ValueError:
        date_part = value.split("T", 1)[0]
    parts = date_part.split("-")
    if len(parts) != 3:
        return value
    return f"{parts[2]}.{parts[1]}.{parts[0]}"


def _old_price(offer: Offer) -> float | None:
    if offer.old_price:
        return offer.old_price
    discount_percent = _discount_percent(offer)
    if offer.price and discount_percent and 0 < discount_percent < 100:
        return round(offer.price / (1 - discount_percent / 100), 2)
    return None


def _discount_percent(offer: Offer) -> float | None:
    if offer.discount_percent:
        return offer.discount_percent
    if offer.old_price and offer.price and offer.old_price > offer.price:
        return round((offer.old_price - offer.price) / offer.old_price * 100, 1)
    return None


def _clean_week(
    value: str | None,
    week_labels: dict[str, dict[str, str]] | None = None,
    default_week: str = "current",
) -> str:
    week_labels = week_labels or WEEK_LABELS
    if value in week_labels:
        return value
    return default_week if default_week in week_labels else next(iter(week_labels))


def _next_week_start(today: date) -> date:
    return today + timedelta(days=7 - today.weekday())


def _starts_in_range(offer: Offer, start: date, end: date) -> bool:
    valid_from = _offer_date(offer.valid_from)
    if valid_from is None:
        return False
    return start <= valid_from <= end


def _is_active_on(offer: Offer, day: date) -> bool:
    valid_from = _offer_date(offer.valid_from)
    valid_to = _offer_date(offer.valid_to)
    if valid_from and valid_from > day:
        return False
    if valid_to and valid_to < day:
        return False
    return True


def _ends_soon(offer: Offer, today: date | None = None) -> bool:
    valid_to = _offer_date(offer.valid_to)
    if valid_to is None:
        return False
    today = today or datetime.now(BERLIN_TZ).date()
    return today <= valid_to <= today + timedelta(days=1)


def _offer_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(BERLIN_TZ)
    return timestamp.date()
