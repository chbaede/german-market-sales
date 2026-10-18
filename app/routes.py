from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Lock
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, current_app, jsonify, make_response, redirect, render_template, request, url_for

from app.config import DRUGSTORE_PAGE, SUPERMARKET_PAGE, WEEK_LABELS
from app.models import Offer, OfferResult
from app.services.basket import BasketRecommendation, calculate_basket_store_totals, recommend_basket_items
from app.services.formatting import (
    format_amount_with_unit as _format_amount,
    format_date_de as _format_date_de,
    format_eur_value as _format_eur,
    format_percent_value as _format_percent,
    parse_offer_date as _offer_date,
)
from app.services.location import (
    QUICK_CITIES,
    get_location_info,
    locate_from_coordinates,
    locate_from_ip,
    normalize_german_plz,
    resolve_plz_city,
)
from app.services.offers import OfferService
from app.services.price_history import (
    build_weekly_competitor_map,
    enrich_weekly_competitors,
    normalize_product_key,
)
from app.services.translations import translate_text

bp = Blueprint("main", __name__)
LOGGER = logging.getLogger(__name__)
BERLIN_TZ = ZoneInfo("Europe/Berlin")
BACKGROUND_REFRESH_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="offer-refresh")
BACKGROUND_REFRESH_LOCK = Lock()
BACKGROUND_REFRESH_KEYS: set[tuple[str, str]] = set()

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


def _get_or_create_offer_service(retailers_key: str, cache_name: str = "offers.json") -> OfferService:
    services: dict[tuple, OfferService] = current_app.extensions.setdefault("offer_services", {})
    cfg = current_app.config
    cache_key = (
        cache_name,
        str(cfg["CACHE_DIR"]),
        cfg["CACHE_TTL_SECONDS"],
        cfg["REQUEST_TIMEOUT_SECONDS"],
        str(cfg["REQUEST_VERIFY_TLS"]),
        cfg["MAX_OFFERS_PER_RETAILER"],
    )
    service = services.get(cache_key)
    if service is None:
        service = OfferService(
            cache_dir=cfg["CACHE_DIR"],
            retailers=cfg[retailers_key],
            cache_ttl_seconds=cfg["CACHE_TTL_SECONDS"],
            timeout=cfg["REQUEST_TIMEOUT_SECONDS"],
            verify_tls=cfg["REQUEST_VERIFY_TLS"],
            max_offers_per_retailer=cfg["MAX_OFFERS_PER_RETAILER"],
            cache_name=cache_name,
        )
        services[cache_key] = service
    return service


def offer_service() -> OfferService:
    return _get_or_create_offer_service("RETAILERS", cache_name="offers.json")


def drugstore_offer_service() -> OfferService:
    return _get_or_create_offer_service("DRUGSTORE_RETAILERS", cache_name="drugstore_offers.json")


def _resolve_request_zip_code() -> tuple[str, bool]:
    explicit_zip = _clean_zip(request.args.get("zip_code") or request.args.get("zip"))
    if explicit_zip:
        normalized = normalize_german_plz(explicit_zip) or explicit_zip
        return normalized, True
    cookie_zip = _clean_zip(request.cookies.get("plz"))
    if cookie_zip:
        normalized = normalize_german_plz(cookie_zip) or cookie_zip
        return normalized, False
    return current_app.config["DEFAULT_ZIP_CODE"], False


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
    zip_code, has_explicit_zip = _resolve_request_zip_code()
    location_info = get_location_info(zip_code)
    has_saved_plz = has_explicit_zip or bool(_clean_zip(request.cookies.get("plz")))
    week_labels = page.get("week_labels", WEEK_LABELS)
    default_week = page.get("default_week", "current")
    if default_week == "current" and datetime.now(BERLIN_TZ).weekday() == 6:
        default_week = "next"
    selected_week = _clean_week(request.args.get("week"), week_labels, default_week)
    result = load_offers_for_request(service, zip_code)

    selected_retailers = _selected_retailers(retailers, page.get("default_retailers", ()))
    query = (request.args.get("q") or "").strip()
    basket_query = (request.args.get("basket") or "").strip()
    category = request.args.get("category") or "all"
    sort = request.args.get("sort") or "essentials"
    priced_only = True
    ending_soon = request.args.get("ending") == "1"
    overlap_only = request.args.get("overlap") == "1"

    priced_offers = [offer for offer in result.offers if offer.price is not None]
    week_offers = enrich_weekly_competitors(filter_offers_by_week(priced_offers, selected_week))
    filtered = filter_offers(week_offers, selected_retailers, query, category, priced_only, ending_soon, overlap_only)
    filtered = sort_offers(filtered, sort)
    filtered = limit_offers_per_retailer(filtered, current_app.config["MAX_OFFERS_PER_RETAILER"])

    _enrich_atl_if_needed(filtered, service, result.fetched_at, zip_code=zip_code)

    rendered = render_template(
        "offers.html",
        offers=filtered,
        basket_query=basket_query,
        stats=build_stats(filtered, week_offers),
        retailers=retailers,
        category_labels=current_app.config["CATEGORY_LABELS"],
        selected_retailers=selected_retailers,
        selected_week=selected_week,
        selected_week_label=week_labels[selected_week],
        week_counts=build_week_counts(
            priced_offers,
            week_labels,
            selected_retailers,
            query,
            category,
            priced_only,
            ending_soon,
            overlap_only,
        ),
        retailer_week_status=build_retailer_week_status(priced_offers, retailers, selected_week, selected_retailers),
        retailer_counts=build_retailer_counts(
            week_offers, retailers, query, category, priced_only, ending_soon, overlap_only
        ),
        default_retailers=page.get("default_retailers", ()),
        week_labels=week_labels,
        week_links=build_week_links(page_endpoint, week_labels),
        selected_category=category,
        selected_sort=sort,
        priced_only=priced_only,
        ending_soon=ending_soon,
        overlap_only=overlap_only,
        query=query,
        zip_code=zip_code,
        location_info=location_info,
        quick_cities=QUICK_CITIES,
        has_saved_plz=has_saved_plz,
        fetched_at=result.fetched_at,
        warnings=result.warnings,
        from_cache=result.from_cache,
        page=page,
        lang=lang,
    )
    response = make_response(rendered)
    if has_explicit_zip:
        response.set_cookie("plz", zip_code, max_age=31536000, path="/", samesite="Lax")
    return response


def _enrich_atl_if_needed(
    offers: list[Offer],
    service: OfferService,
    fetched_at: str | None,
    zip_code: str | None = None,
) -> None:
    if not offers:
        return
    all_time_low_map = service.get_all_time_low_map(zip_code=zip_code) if hasattr(service, "get_all_time_low_map") else {}
    national_atl_map = service.get_all_time_low_map(zip_code=None) if hasattr(service, "get_all_time_low_map") else {}
    for offer in offers:
        if not offer.product_key:
            offer.product_key = normalize_product_key(offer.title, offer.brand)
        min_p = all_time_low_map.get(offer.product_key)
        nat_min_p = national_atl_map.get(offer.product_key)
        is_atl = bool(offer.price is not None and min_p is not None and offer.price <= (min_p + 0.001))
        is_nat_atl = bool(offer.price is not None and nat_min_p is not None and offer.price <= (nat_min_p + 0.001))
        offer.all_time_low = min_p
        offer.is_all_time_low = is_atl
        offer.regional_all_time_low = min_p
        offer.is_regional_all_time_low = is_atl
        offer.national_all_time_low = nat_min_p
        offer.is_national_all_time_low = is_nat_atl
        offer.recorded_at = fetched_at


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
    zip_code = _resolve_request_zip_code()[0]
    selected_week = _clean_week(request.args.get("week"), week_labels, default_week)
    result = load_offers_for_request(service, zip_code)
    offers = filter_offers_by_week(result.offers, selected_week)
    _enrich_atl_if_needed(offers, service, result.fetched_at, zip_code=zip_code)
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


@bp.get("/api/locate")
def api_locate():
    lat_raw = request.args.get("lat")
    lon_raw = request.args.get("lon")
    zip_raw = request.args.get("zip_code")
    city_hint = (request.args.get("city") or "").strip() or None

    result: dict | None = None
    if lat_raw is not None and lon_raw is not None:
        try:
            result = locate_from_coordinates(float(lat_raw), float(lon_raw))
        except ValueError:
            result = None

    if result is None and (zip_raw or city_hint):
        normalized = normalize_german_plz(_clean_zip(zip_raw), city_hint=city_hint)
        if normalized:
            city = resolve_plz_city(normalized, allow_remote=True)
            result = {
                "zip_code": normalized,
                "city": city,
                "detected": True,
                "source": "zip",
            }

    if result is None:
        forwarded = (
            request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.headers.get("X-Real-IP")
            or request.remote_addr
        )
        result = locate_from_ip(forwarded)

    if result is None:
        default_zip = current_app.config["DEFAULT_ZIP_CODE"]
        return jsonify(
            {
                "zip_code": default_zip,
                "city": resolve_plz_city(default_zip),
                "detected": False,
                "source": "default",
            }
        )

    response = make_response(jsonify(result))
    response.set_cookie("plz", result["zip_code"], max_age=31536000, path="/", samesite="Lax")
    return response


@bp.get("/api/basket")
def api_basket():
    return api_basket_for(offer_service(), WEEK_LABELS)


@bp.get("/api/drogerie/basket")
def api_drugstore_basket():
    return api_basket_for(drugstore_offer_service(), WEEK_LABELS)


def api_basket_for(service: OfferService, week_labels: dict[str, dict[str, str]], default_week: str = "current"):
    zip_code = _resolve_request_zip_code()[0]
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
    retailers = getattr(service, "retailers", [])
    store_totals = calculate_basket_store_totals(recommendations, retailers)
    return jsonify(
        {
            "zip_code": zip_code,
            "week": selected_week,
            "week_label": week_labels[selected_week],
            "retailers": sorted(selected_retailers),
            "query": basket_query,
            "recommendations": [_basket_recommendation_to_dict(item) for item in recommendations],
            "store_totals": store_totals,
        }
    )


@bp.get("/api/price-history")
def api_price_history():
    q = (request.args.get("q") or "").strip()
    brand = (request.args.get("brand") or "").strip() or None
    price_val = request.args.get("price")
    current_price = None
    if price_val:
        try:
            current_price = float(price_val)
        except ValueError:
            current_price = None

    zip_code = _resolve_request_zip_code()[0]
    service = offer_service()
    stats = service.price_history.get_product_stats(q, brand, current_price, zip_code=zip_code)
    return jsonify(stats)


@bp.get("/api/diagnostics")
def api_diagnostics():
    zip_code = _clean_zip(request.args.get("zip_code") or request.args.get("zip"))
    if not zip_code:
        zip_code = _resolve_request_zip_code()[0]
    retailer = request.args.get("retailer")
    service_type = request.args.get("type", "supermarket")
    service = drugstore_offer_service() if service_type == "drugstore" else offer_service()
    diag = service.get_regional_diagnostics(zip_code, retailer_slug=retailer)
    return jsonify(diag)


def _load_combined_week_offers(zip_code: str, selected_week: str) -> list[Offer]:
    supermarket_res = load_offers_for_request(offer_service(), zip_code)
    drugstore_res = load_offers_for_request(drugstore_offer_service(), zip_code)
    return filter_offers_by_week(supermarket_res.offers + drugstore_res.offers, selected_week)


@bp.get("/api/compare")
def api_compare():
    q = (request.args.get("q") or "").strip()
    zip_code = _resolve_request_zip_code()[0]
    selected_week = _clean_week(request.args.get("week"), WEEK_LABELS, "current")
    all_offers = _load_combined_week_offers(zip_code, selected_week)
    result = offer_service().price_history.compare_across_stores(q, all_offers)
    return jsonify(result)


@bp.route("/api/watchlist-deals", methods=["GET", "POST"])
def api_watchlist_deals():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        items = data.get("items") or data.get("watchlist") or []
        zip_code = _clean_zip(data.get("zip_code")) or _resolve_request_zip_code()[0]
        selected_week = _clean_week(data.get("week"), WEEK_LABELS, "current")
    else:
        kw = request.args.get("keywords") or ""
        items = [{"name": part.strip()} for part in kw.split(",") if part.strip()]
        zip_code = _resolve_request_zip_code()[0]
        selected_week = _clean_week(request.args.get("week"), WEEK_LABELS, "current")

    all_offers = _load_combined_week_offers(zip_code, selected_week)
    return jsonify(offer_service().price_history.match_watchlist_deals(items, all_offers))


@bp.get("/health")
def health():
    return {"status": "ok"}


@bp.get("/api/scheduler/status")
def api_scheduler_status():
    from app.services.scheduler import AutoRefreshScheduler

    scheduler = current_app.extensions.get("auto_scheduler")
    if scheduler:
        status = scheduler.get_status()
        status["scheduler_running"] = scheduler._thread is not None and scheduler._thread.is_alive()
        status["check_interval_seconds"] = scheduler.check_interval_seconds
        status["max_cache_age_hours"] = scheduler.max_cache_age_hours
        return jsonify(status)
    dummy = AutoRefreshScheduler(current_app)
    status = dummy.get_status()
    status["scheduler_running"] = False
    return jsonify(status)


@bp.post("/api/scheduler/trigger")
def api_scheduler_trigger():
    scheduler = current_app.extensions.get("auto_scheduler")
    if scheduler:
        return jsonify(scheduler.execute_refresh())
    from app.services.scheduler import AutoRefreshScheduler

    runner = AutoRefreshScheduler(current_app)
    return jsonify(runner.execute_refresh())


@bp.get("/ads.txt")
def ads_txt():
    return current_app.send_static_file("ads.txt")


@bp.get("/robots.txt")
def robots_txt():
    base_url = request.url_root.rstrip("/")
    content = f"""User-agent: *
Allow: /
Disallow: /api/
Disallow: /refresh
Disallow: /drogerie/refresh

Sitemap: {base_url}/sitemap.xml
"""
    return Response(content, mimetype="text/plain; charset=utf-8")


@bp.get("/sitemap.xml")
def sitemap_xml():
    base_url = request.url_root.rstrip("/")
    now = datetime.now(BERLIN_TZ).strftime("%Y-%m-%d")
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{base_url}/</loc>
    <lastmod>{now}</lastmod>
    <changefreq>daily</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>{base_url}/drogerie</loc>
    <lastmod>{now}</lastmod>
    <changefreq>daily</changefreq>
    <priority>0.9</priority>
  </url>
</urlset>
"""
    return Response(xml_content, mimetype="application/xml; charset=utf-8")


def filter_offers(
    offers: list[Offer],
    selected_retailers: set[str],
    query: str,
    category: str,
    priced_only: bool = False,
    ending_soon: bool = False,
    overlap_only: bool = False,
) -> list[Offer]:
    query_key = query.casefold()
    today = datetime.now(BERLIN_TZ).date() if ending_soon else None
    filtered: list[Offer] = []
    for offer in offers:
        if selected_retailers and offer.retailer_slug not in selected_retailers:
            continue
        if category != "all" and offer.category != category:
            continue
        if priced_only and offer.price is None and offer.unit_price is None:
            continue
        if ending_soon and not _ends_soon(offer, today=today):
            continue
        if overlap_only and not getattr(offer, "competitors", None):
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
        return [offer for offer in offers if _is_relevant_for_next_week(offer, start, end)]
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
    today = datetime.now(BERLIN_TZ).date()
    discounted = [offer for offer in filtered if offer.discount_percent]
    best = max((offer.discount_percent or 0 for offer in filtered), default=0)
    return {
        "visible": len(filtered),
        "total": len(all_offers),
        "discounted": len(discounted),
        "best_discount": round(best),
        "ending_soon": sum(1 for offer in filtered if _ends_soon(offer, today=today)),
        "all_time_low_count": sum(1 for offer in filtered if getattr(offer, "is_all_time_low", False)),
        "overlapping_count": sum(1 for offer in filtered if getattr(offer, "competitors", None)),
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
    overlap_only: bool = False,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for week in week_labels:
        week_subset = filter_offers_by_week(offers, week)
        if overlap_only:
            enrich_weekly_competitors(week_subset)
        counts[week] = len(
            filter_offers(
                week_subset,
                selected_retailers or set(),
                query,
                category,
                priced_only,
                ending_soon,
                overlap_only,
            )
        )
    return counts


def build_retailer_counts(
    offers: list[Offer],
    retailers: dict[str, str],
    query: str = "",
    category: str = "all",
    priced_only: bool = False,
    ending_soon: bool = False,
    overlap_only: bool = False,
) -> dict[str, int]:
    counts = {slug: 0 for slug in retailers}
    for offer in filter_offers(offers, set(), query, category, priced_only, ending_soon, overlap_only):
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
        service.get_offers(zip_code=zip_code, refresh=False)
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


def _format_unit_price(offer: Offer) -> str | None:
    if offer.unit_price is None:
        return None
    return f"{_format_eur(offer.unit_price)} / {offer.unit or 'unit'}"


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


def _is_relevant_for_next_week(offer: Offer, start: date, end: date) -> bool:
    valid_from = _offer_date(offer.valid_from)
    valid_to = _offer_date(offer.valid_to)
    if valid_from and valid_from > end:
        return False
    if valid_to and valid_to < start:
        return False
    if valid_from and start <= valid_from <= end:
        return True
    if valid_to and valid_to >= start:
        return True
    return False


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
