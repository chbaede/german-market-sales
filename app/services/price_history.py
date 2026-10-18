from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from functools import lru_cache
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
from zoneinfo import ZoneInfo

from app.models import Offer

BERLIN_TZ = ZoneInfo("Europe/Berlin")

STOP_WORDS = frozenset(
    {
        "versch",
        "sorten",
        "verschiedene",
        "original",
        "extra",
        "packung",
        "flasche",
        "dose",
        "becher",
        "tafel",
        "glas",
        "beutel",
        "karton",
        "schale",
        "stuck",
        "stueck",
        "aktion",
        "knaller",
        "superpreis",
        "rabatt",
        "angebot",
        "g&g",
        "gut&gunstig",
        "ja!",
        "rewe",
        "edeka",
        "milbona",
        "ja",
    }
)

QUANTITY_REGEX = re.compile(
    r"\b\d+(?:[,.]\d+)?\s*(?:kg|g|ml|l|liter|stk|stuck|stueck|er|teilig|tl|x\s*\d+)?\b",
    re.IGNORECASE,
)
QUANTITY_NORM_REGEX = re.compile(
    r"\b(\d+(?:[.,]\d+)?)\s*(kg|g|ml|l|liter|stk|stueck|stuck)\b",
    re.IGNORECASE,
)
NON_ALNUM_REGEX = re.compile(r"[^a-z0-9\s]")
WHITESPACE_REGEX = re.compile(r"\s+")


def _extract_normalized_quantity(text: str) -> str | None:
    match = QUANTITY_NORM_REGEX.search(text)
    if not match:
        return None
    val_str, unit_str = match.groups()
    try:
        val = float(val_str.replace(",", "."))
    except ValueError:
        return None
    unit = unit_str.lower()
    if unit == "kg":
        return f"{int(round(val * 1000))}g"
    elif unit == "g":
        return f"{int(round(val))}g"
    elif unit in ("l", "liter"):
        return f"{int(round(val * 1000))}ml"
    elif unit == "ml":
        return f"{int(round(val))}ml"
    elif unit in ("stk", "stuck", "stueck"):
        return f"{int(round(val))}stk"
    return None


@lru_cache(maxsize=4096)
def normalize_product_key(title: str, brand: str | None = None) -> str:
    combined = f"{brand or ''} {title}".strip()
    norm_qty = _extract_normalized_quantity(combined)
    text = unicodedata.normalize("NFKD", combined)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("ß", "ss").casefold()

    text = QUANTITY_REGEX.sub(" ", text)
    text = NON_ALNUM_REGEX.sub(" ", text)
    words = [word for word in text.split() if len(word) >= 2 and word not in STOP_WORDS]
    if not words:
        base = WHITESPACE_REGEX.sub("-", text.strip()) or "item"
    else:
        base = "-".join(sorted(set(words[:5])))
    if norm_qty:
        return f"{base}_{norm_qty}"
    return base


def get_geographic_identity(offer: Offer, fallback_zip: str) -> tuple[str, str, str]:
    """
    Returns (scope_type, geo_id, effective_zip) ensuring consistent identity per scope:
    - store: ('store', store_id or effective_zip, effective_zip)
    - region: ('region', region_id or effective_zip, effective_zip)
    - nationwide: ('nationwide', 'DE', 'DE')
    - unknown: ('unknown', effective_zip, effective_zip)
    """
    scope = offer.scope_type or "unknown"
    effective_zip = offer.requested_zip_code or fallback_zip
    if scope == "store":
        geo_id = offer.store_id or offer.store_name or effective_zip
        return ("store", geo_id, effective_zip)
    elif scope == "region":
        geo_id = offer.source_region_id or effective_zip
        return ("region", geo_id, effective_zip)
    elif scope == "nationwide":
        return ("nationwide", "DE", "DE")
    else:
        return ("unknown", effective_zip, effective_zip)


class PriceHistoryService:
    _INITIALIZED_DBS: set[Path] = set()
    _SEEDED_DBS: set[Path] = set()

    def __init__(self, cache_dir: Path, db_name: str = "price_history.db") -> None:
        self.cache_dir = cache_dir
        self.db_path = (cache_dir / db_name).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._atl_cache: tuple[int, dict[str, float]] | None = None
        if self.db_path not in self._INITIALIZED_DBS or not self.db_path.exists():
            self._init_db()
            self._INITIALIZED_DBS.add(self.db_path)

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    brand TEXT,
                    category TEXT,
                    retailer_slug TEXT NOT NULL,
                    retailer_name TEXT NOT NULL,
                    price REAL,
                    old_price REAL,
                    unit_price REAL,
                    unit TEXT,
                    discount_percent REAL,
                    recorded_at TEXT NOT NULL,
                    valid_from TEXT,
                    valid_to TEXT,
                    source_url TEXT,
                    zip_code TEXT
                )
                """
            )
            # Safe migration for existing tables:
            cursor = conn.execute("PRAGMA table_info(price_history)")
            existing_cols = {row["name"] for row in cursor.fetchall()}
            new_cols = {
                "scope_type": "TEXT DEFAULT 'unknown'",
                "store_id": "TEXT",
                "store_name": "TEXT",
                "region_verified": "INTEGER DEFAULT 0",
                "source_provider": "TEXT",
                "source_region_id": "TEXT",
                "verification_method": "TEXT",
            }
            for col_name, col_type in new_cols.items():
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE price_history ADD COLUMN {col_name} {col_type}")

            conn.execute("CREATE INDEX IF NOT EXISTS idx_ph_key ON price_history(product_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ph_date ON price_history(recorded_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ph_retailer ON price_history(retailer_slug)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ph_zip ON price_history(zip_code)")
            conn.commit()

    def record_offers(
        self,
        offers: list[Offer],
        zip_code: str,
        recorded_at: str | None = None,
    ) -> int:
        if not offers:
            return 0

        timestamp = recorded_at or datetime.now(UTC).isoformat(timespec="seconds")
        date_prefix = timestamp.split("T", 1)[0]

        with self._get_connection() as conn:
            existing_rows = conn.execute(
                """
                SELECT product_key, retailer_slug, COALESCE(scope_type, 'unknown') as scope_type,
                       COALESCE(store_id, '') as store_id, COALESCE(source_region_id, '') as source_region_id,
                       COALESCE(zip_code, '') as zip_code, price
                FROM price_history
                WHERE recorded_at LIKE ? AND price IS NOT NULL
                """,
                (f"{date_prefix}%",),
            ).fetchall()
            seen_today: set[tuple[str, str, str, str, float]] = set()
            for row in existing_rows:
                s_type = row["scope_type"]
                if s_type == "store":
                    g_id = row["store_id"]
                elif s_type == "region":
                    g_id = row["source_region_id"] or row["zip_code"]
                elif s_type == "nationwide":
                    g_id = "DE"
                else:
                    g_id = row["zip_code"]
                seen_today.add((
                    row["product_key"],
                    row["retailer_slug"],
                    s_type,
                    g_id,
                    round(float(row["price"]), 4),
                ))

            rows_to_insert: list[tuple] = []
            for offer in offers:
                if offer.price is None:
                    continue

                product_key = offer.product_key or normalize_product_key(offer.title, offer.brand)
                scope_type, geo_id, effective_zip = get_geographic_identity(offer, zip_code)
                dedup_key = (
                    product_key,
                    offer.retailer_slug,
                    scope_type,
                    geo_id,
                    round(float(offer.price), 4),
                )
                if dedup_key in seen_today:
                    continue
                seen_today.add(dedup_key)

                rows_to_insert.append(
                    (
                        product_key,
                        offer.title,
                        offer.brand,
                        offer.category,
                        offer.retailer_slug,
                        offer.retailer,
                        offer.price,
                        offer.old_price,
                        offer.unit_price,
                        offer.unit,
                        offer.discount_percent,
                        timestamp,
                        offer.valid_from,
                        offer.valid_to,
                        offer.source_url,
                        effective_zip,
                        scope_type,
                        offer.store_id,
                        offer.store_name,
                        1 if offer.region_verified else 0,
                        offer.source_provider,
                        offer.source_region_id,
                        offer.verification_method,
                    )
                )

            if rows_to_insert:
                conn.executemany(
                    """
                    INSERT INTO price_history (
                        product_key, title, brand, category, retailer_slug, retailer_name,
                        price, old_price, unit_price, unit, discount_percent, recorded_at,
                        valid_from, valid_to, source_url, zip_code,
                        scope_type, store_id, store_name, region_verified, source_provider,
                        source_region_id, verification_method
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows_to_insert,
                )
                conn.commit()
                self._atl_cache = None

        return len(rows_to_insert)

    def get_all_time_low_map(self, zip_code: str | None = None, store_id: str | None = None) -> dict[str, float]:
        with self._get_connection() as conn:
            if zip_code:
                if store_id:
                    store_clause = "(scope_type = 'store' AND region_verified = 1 AND store_id = ?)"
                    params = (store_id, zip_code)
                else:
                    store_clause = "(scope_type = 'store' AND region_verified = 1 AND zip_code = ?)"
                    params = (zip_code, zip_code)
                query = f"""
                    SELECT product_key, MIN(price) as min_price
                    FROM price_history
                    WHERE price IS NOT NULL AND price > 0
                      AND (
                          {store_clause}
                          OR (scope_type = 'region' AND region_verified = 1 AND zip_code = ?)
                          OR scope_type = 'nationwide'
                      )
                    GROUP BY product_key
                """
                cursor = conn.execute(query, params)
            else:
                cursor = conn.execute(
                    """
                    SELECT product_key, MIN(price) as min_price
                    FROM price_history
                    WHERE price IS NOT NULL AND price > 0
                      AND (
                          (scope_type = 'store' AND region_verified = 1)
                          OR (scope_type = 'region' AND region_verified = 1)
                          OR scope_type = 'nationwide'
                      )
                    GROUP BY product_key
                    """
                )
            return {row["product_key"]: float(row["min_price"]) for row in cursor.fetchall()}

    def get_product_stats(
        self,
        title: str,
        brand: str | None = None,
        current_price: float | None = None,
        zip_code: str | None = None,
        store_id: str | None = None,
    ) -> dict:
        product_key = normalize_product_key(title, brand)
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT
                    MIN(price) as min_price,
                    MAX(price) as max_price,
                    AVG(price) as avg_price,
                    COUNT(*) as count
                FROM price_history
                WHERE product_key = ? AND price IS NOT NULL
                  AND (
                      (scope_type = 'store' AND region_verified = 1)
                      OR (scope_type = 'region' AND region_verified = 1)
                      OR scope_type = 'nationwide'
                  )
                """,
                (product_key,),
            )
            agg = cursor.fetchone()

            min_price = float(agg["min_price"]) if agg and agg["min_price"] is not None else current_price
            max_price = float(agg["max_price"]) if agg and agg["max_price"] is not None else current_price
            avg_price = float(round(agg["avg_price"], 2)) if agg and agg["avg_price"] is not None else current_price
            record_count = int(agg["count"]) if agg else 0

            regional_min = None
            regional_avg = None
            is_regional_atl = False
            if zip_code:
                if store_id:
                    store_clause = "(scope_type = 'store' AND region_verified = 1 AND store_id = ?)"
                    r_params = (product_key, store_id, zip_code)
                else:
                    store_clause = "(scope_type = 'store' AND region_verified = 1 AND zip_code = ?)"
                    r_params = (product_key, zip_code, zip_code)
                r_query = f"""
                    SELECT
                        MIN(price) as min_price,
                        AVG(price) as avg_price
                    FROM price_history
                    WHERE product_key = ? AND price IS NOT NULL
                      AND (
                          {store_clause}
                          OR (scope_type = 'region' AND region_verified = 1 AND zip_code = ?)
                          OR scope_type = 'nationwide'
                      )
                """
                r_cursor = conn.execute(r_query, r_params)
                r_agg = r_cursor.fetchone()
                if r_agg and r_agg["min_price"] is not None:
                    regional_min = float(r_agg["min_price"])
                    regional_avg = float(round(r_agg["avg_price"], 2))
                    if current_price is not None:
                        is_regional_atl = current_price <= (regional_min + 0.001)

            # Fetch recent price observation points: strictly isolate by scope / location
            if zip_code:
                if store_id:
                    store_p_clause = "(scope_type = 'store' AND region_verified = 1 AND store_id = ?)"
                    p_params = (product_key, store_id, zip_code)
                else:
                    store_p_clause = "(scope_type = 'store' AND region_verified = 1 AND zip_code = ?)"
                    p_params = (product_key, zip_code, zip_code)
                p_query = f"""
                    SELECT
                        recorded_at, price, old_price, retailer_name, retailer_slug,
                        source_url, unit_price, unit, discount_percent, zip_code, scope_type, store_name,
                        store_id, region_verified
                    FROM price_history
                    WHERE product_key = ? AND price IS NOT NULL
                      AND (
                          {store_p_clause}
                          OR (scope_type = 'region' AND region_verified = 1 AND zip_code = ?)
                          OR scope_type = 'nationwide'
                      )
                    ORDER BY recorded_at ASC
                    LIMIT 30
                """
                points_cursor = conn.execute(p_query, p_params)
            else:
                p_query = """
                    SELECT
                        recorded_at, price, old_price, retailer_name, retailer_slug,
                        source_url, unit_price, unit, discount_percent, zip_code, scope_type, store_name,
                        store_id, region_verified
                    FROM price_history
                    WHERE product_key = ? AND price IS NOT NULL
                    ORDER BY recorded_at ASC
                    LIMIT 30
                """
                points_cursor = conn.execute(p_query, (product_key,))

            history = [
                {
                    "date": row["recorded_at"].split("T", 1)[0],
                    "recorded_at": row["recorded_at"],
                    "price": float(row["price"]),
                    "old_price": float(row["old_price"]) if row["old_price"] is not None else None,
                    "retailer": row["retailer_name"],
                    "retailer_slug": row["retailer_slug"],
                    "source_url": row["source_url"],
                    "unit_price": row["unit_price"],
                    "unit": row["unit"],
                    "discount_percent": row["discount_percent"],
                    "zip_code": row["zip_code"],
                    "scope_type": row["scope_type"],
                    "store_name": row["store_name"],
                    "store_id": row["store_id"],
                    "region_verified": bool(row["region_verified"]),
                }
                for row in points_cursor.fetchall()
            ]

        is_all_time_low = False
        if current_price is not None and min_price is not None:
            is_all_time_low = current_price <= (min_price + 0.001)

        # For backward compatibility, all_time_low reflects regional if zip_code given and available
        display_atl = regional_min if (zip_code and regional_min is not None) else min_price
        display_is_atl = is_regional_atl if (zip_code and regional_min is not None) else is_all_time_low

        return {
            "product_key": product_key,
            "title": title,
            "product_name": title,
            "brand": brand,
            "current_price": current_price,
            "all_time_low": display_atl,
            "all_time_high": max_price,
            "average_price": regional_avg if (zip_code and regional_avg is not None) else avg_price,
            "avg_price": regional_avg if (zip_code and regional_avg is not None) else avg_price,
            "regional_all_time_low": regional_min,
            "national_all_time_low": min_price,
            "is_all_time_low": display_is_atl,
            "is_regional_all_time_low": is_regional_atl,
            "is_national_all_time_low": is_all_time_low,
            "record_count": record_count,
            "records_count": record_count,
            "history": history,
        }

    def compare_across_stores(self, query: str, offers: list[Offer]) -> dict:
        query_key = query.casefold().strip()
        tokens = [t for t in WHITESPACE_REGEX.split(query_key) if len(t) >= 2] or [query_key]

        matching: list[Offer] = []
        for offer in offers:
            haystack = f"{offer.title} {offer.brand or ''} {offer.description}".casefold()
            if all(token in haystack for token in tokens):
                matching.append(offer)

        by_retailer: dict[str, list[dict]] = {}
        for offer in matching:
            by_retailer.setdefault(offer.retailer, []).append(
                {
                    "id": offer.id,
                    "title": offer.title,
                    "brand": offer.brand,
                    "retailer": offer.retailer,
                    "retailer_slug": offer.retailer_slug,
                    "price": offer.price,
                    "old_price": offer.old_price,
                    "unit_price": offer.unit_price,
                    "unit": offer.unit,
                    "discount_percent": offer.discount_percent,
                    "image_url": offer.image_url,
                    "source_url": offer.source_url,
                    "valid_from": offer.valid_from,
                    "valid_to": offer.valid_to,
                }
            )

        priced = [o for o in matching if o.price is not None and o.price > 0]
        cheapest_offer = min(priced, key=lambda o: o.price) if priced else None
        most_expensive = max(priced, key=lambda o: o.price) if priced else None

        min_price = cheapest_offer.price if cheapest_offer else None
        max_price = most_expensive.price if most_expensive else None
        avg_price = round(sum(o.price for o in priced) / len(priced), 2) if priced else None
        max_savings = round(max_price - min_price, 2) if min_price and max_price else 0.0
        savings_pct = (
            round((max_savings / max_price) * 100, 1)
            if (max_price and max_price > 0 and max_savings > 0)
            else 0.0
        )

        stores = [
            min(offers_list, key=lambda o: (o["price"] is None, o["price"] or 999999))
            for offers_list in by_retailer.values()
        ]
        stores.sort(key=lambda s: (s["price"] is None, s["price"] or 999999))

        cheapest_dict = (
            {"retailer": cheapest_offer.retailer, "price": cheapest_offer.price, "title": cheapest_offer.title}
            if cheapest_offer
            else None
        )

        return {
            "query": query,
            "total_matches": len(matching),
            "stores_count": len(by_retailer),
            "cheapest_store": cheapest_dict,
            "cheapest_store_name": cheapest_offer.retailer if cheapest_offer else None,
            "cheapest_price": min_price,
            "highest_price": max_price,
            "average_price": avg_price,
            "potential_savings": max_savings,
            "max_savings": max_savings,
            "savings_percent": savings_pct,
            "by_retailer": by_retailer,
            "stores": stores,
        }

    def match_watchlist_deals(self, items: list[dict], offers: list[Offer]) -> dict:
        matches = []
        for item in items:
            item_id = item.get("id") or ""
            name = (item.get("name") or item.get("keyword") or "").strip()
            if not name:
                continue
            raw_target = item.get("target_price")
            target_price = None
            if raw_target is not None:
                try:
                    target_price = float(raw_target)
                except (ValueError, TypeError):
                    target_price = None

            tokens = [t.casefold() for t in WHITESPACE_REGEX.split(name) if len(t) >= 2] or [name.casefold()]

            item_matches = []
            for offer in offers:
                haystack = f"{offer.title} {offer.brand or ''} {offer.description}".casefold()
                if all(tok in haystack for tok in tokens):
                    is_hit = bool(target_price is not None and offer.price is not None and offer.price <= target_price)
                    item_matches.append(
                        {
                            "id": offer.id,
                            "retailer": offer.retailer,
                            "retailer_slug": offer.retailer_slug,
                            "title": offer.title,
                            "price": offer.price,
                            "old_price": offer.old_price,
                            "discount_percent": offer.discount_percent,
                            "unit_price": offer.unit_price,
                            "unit": offer.unit,
                            "valid_from": offer.valid_from,
                            "valid_to": offer.valid_to,
                            "source_url": offer.source_url,
                            "is_target_hit": is_hit,
                        }
                    )

            item_matches.sort(key=lambda m: (m["price"] is None, m["price"] or 999999))
            matches.append(
                {
                    "id": item_id,
                    "name": name,
                    "keyword": name,
                    "target_price": target_price,
                    "deals_count": len(item_matches),
                    "best_deal": item_matches[0] if item_matches else None,
                    "all_deals": item_matches[:5],
                    "matches": item_matches[:5],
                }
            )

        return {
            "items": matches,
            "results": matches,
            "total_active_alerts": sum(1 for m in matches if m["deals_count"] > 0),
        }

    def seed_initial_history(self) -> int:
        if self.db_path in self._SEEDED_DBS and self.db_path.exists():
            return 0

        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) as count FROM price_history")
            count = cursor.fetchone()["count"]
            if count > 50:
                self._SEEDED_DBS.add(self.db_path)
                return 0

        imported = 0
        now = datetime.now(UTC)
        past_week = (now - timedelta(days=7)).isoformat(timespec="seconds")
        current_iso = now.isoformat(timespec="seconds")

        for filename in ("offers.json", "drugstore_offers.json"):
            file_path = self.cache_dir / filename
            if not file_path.exists():
                continue
            try:
                with file_path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                offers_data = data.get("offers", [])
                zip_code = data.get("zip_code", "14195")
                offers = [Offer.from_dict(item) for item in offers_data]

                # Seed current week observations
                imported += self.record_offers(offers, zip_code=zip_code, recorded_at=current_iso)

                # Seed historical anchor points from old_price to build initial baseline trend
                past_offers: list[Offer] = [
                    Offer(
                        id=f"hist-{offer.id}",
                        source_offer_id=offer.source_offer_id,
                        retailer=offer.retailer,
                        retailer_slug=offer.retailer_slug,
                        title=offer.title,
                        brand=offer.brand,
                        category=offer.category,
                        price=offer.old_price,
                        old_price=None,
                        unit_price=None,
                        unit=offer.unit,
                        discount_percent=None,
                        description=offer.description,
                        valid_from=None,
                        valid_to=None,
                        image_url=offer.image_url,
                        source_url=offer.source_url,
                        product_key=offer.product_key,
                    )
                    for offer in offers
                    if offer.old_price and offer.old_price > 0
                ]
                if past_offers:
                    imported += self.record_offers(past_offers, zip_code=zip_code, recorded_at=past_week)
            except Exception:
                continue

        if imported > 0:
            self._SEEDED_DBS.add(self.db_path)
        return imported


def build_weekly_competitor_map(offers: list[Offer]) -> dict[str, list[dict]]:
    """
    Finds competing offers for the same product at other retailers in the same week.
    Returns a dictionary mapping offer.id -> list of competitor dicts sorted by price.
    """
    by_product_key: dict[str, list[Offer]] = defaultdict(list)
    for o in offers:
        pkey = o.product_key or normalize_product_key(o.title, o.brand)
        o.product_key = pkey
        by_product_key[pkey].append(o)

    competitor_map: dict[str, list[dict]] = defaultdict(list)
    for p_offers in by_product_key.values():
        distinct_retailers = {o.retailer_slug for o in p_offers}
        if len(distinct_retailers) <= 1:
            continue

        for current in p_offers:
            comp_list = []
            for other in p_offers:
                if other.retailer_slug == current.retailer_slug:
                    continue
                diff = None
                if current.price is not None and other.price is not None:
                    diff = round(current.price - other.price, 2)
                comp_list.append(
                    {
                        "id": other.id,
                        "retailer": other.retailer,
                        "retailer_slug": other.retailer_slug,
                        "title": other.title,
                        "price": other.price,
                        "old_price": other.old_price,
                        "unit_price": other.unit_price,
                        "unit": other.unit,
                        "discount_percent": other.discount_percent,
                        "source_url": other.source_url,
                        "price_diff": diff,
                    }
                )
            comp_list.sort(key=lambda c: (c["price"] is None, c["price"] or 999999))
            competitor_map[current.id] = comp_list

    return competitor_map


def enrich_weekly_competitors(offers: list[Offer]) -> list[Offer]:
    """
    Populates product_key, competitors, cheapest_competitor, and is_cheapest_in_week
    on a list of weekly offers in place and returns the list.
    """
    competitor_map = build_weekly_competitor_map(offers)
    for offer in offers:
        comps = competitor_map.get(offer.id, [])
        offer.competitors = comps
        priced_comps = [c for c in comps if c["price"] is not None]
        offer.cheapest_competitor = priced_comps[0] if priced_comps else None
        is_cheapest = False
        if comps and offer.price is not None:
            is_cheapest = all(c["price"] is None or offer.price <= (c["price"] + 0.001) for c in comps)
        offer.is_cheapest_in_week = is_cheapest
    return offers
