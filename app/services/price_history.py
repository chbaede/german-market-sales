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
    r"\b\d+(?:[,.]\d+)?\s*(?:kg|g|ml|l|liter|stk|stuck|er|teilig|tl|x\s*\d+)?\b",
    re.IGNORECASE,
)
NON_ALNUM_REGEX = re.compile(r"[^a-z0-9\s]")
WHITESPACE_REGEX = re.compile(r"\s+")


@lru_cache(maxsize=4096)
def normalize_product_key(title: str, brand: str | None = None) -> str:
    combined = f"{brand or ''} {title}".strip()
    text = unicodedata.normalize("NFKD", combined)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("ß", "ss").casefold()

    text = QUANTITY_REGEX.sub(" ", text)
    text = NON_ALNUM_REGEX.sub(" ", text)
    words = [word for word in text.split() if len(word) >= 2 and word not in STOP_WORDS]
    if not words:
        return WHITESPACE_REGEX.sub("-", text.strip()) or "item"
    return "-".join(sorted(set(words[:5])))


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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ph_key ON price_history(product_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ph_date ON price_history(recorded_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ph_retailer ON price_history(retailer_slug)")
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
                SELECT product_key, retailer_slug, price
                FROM price_history
                WHERE recorded_at LIKE ? AND price IS NOT NULL
                """,
                (f"{date_prefix}%",),
            ).fetchall()
            seen_today: set[tuple[str, str, float]] = {
                (row["product_key"], row["retailer_slug"], round(float(row["price"]), 4))
                for row in existing_rows
            }

            rows_to_insert: list[tuple] = []
            for offer in offers:
                if offer.price is None:
                    continue

                product_key = offer.product_key or normalize_product_key(offer.title, offer.brand)
                dedup_key = (product_key, offer.retailer_slug, round(float(offer.price), 4))
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
                        zip_code,
                    )
                )

            if rows_to_insert:
                conn.executemany(
                    """
                    INSERT INTO price_history (
                        product_key, title, brand, category, retailer_slug, retailer_name,
                        price, old_price, unit_price, unit, discount_percent, recorded_at,
                        valid_from, valid_to, source_url, zip_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows_to_insert,
                )
                conn.commit()
                self._atl_cache = None

        return len(rows_to_insert)

    def get_all_time_low_map(self) -> dict[str, float]:
        mtime_ns = self.db_path.stat().st_mtime_ns if self.db_path.exists() else 0
        if self._atl_cache is not None and self._atl_cache[0] == mtime_ns:
            return self._atl_cache[1]

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT product_key, MIN(price) as min_price
                FROM price_history
                WHERE price IS NOT NULL AND price > 0
                GROUP BY product_key
                """
            )
            atl_map = {row["product_key"]: float(row["min_price"]) for row in cursor.fetchall()}

        self._atl_cache = (mtime_ns, atl_map)
        return atl_map

    def get_product_stats(
        self,
        title: str,
        brand: str | None = None,
        current_price: float | None = None,
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
                """,
                (product_key,),
            )
            agg = cursor.fetchone()

            min_price = float(agg["min_price"]) if agg and agg["min_price"] is not None else current_price
            max_price = float(agg["max_price"]) if agg and agg["max_price"] is not None else current_price
            avg_price = float(round(agg["avg_price"], 2)) if agg and agg["avg_price"] is not None else current_price
            record_count = int(agg["count"]) if agg else 0

            # Fetch recent price observation points
            points_cursor = conn.execute(
                """
                SELECT
                    recorded_at, price, old_price, retailer_name, retailer_slug,
                    source_url, unit_price, unit, discount_percent
                FROM price_history
                WHERE product_key = ? AND price IS NOT NULL
                ORDER BY recorded_at ASC
                LIMIT 30
                """,
                (product_key,),
            )
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
                }
                for row in points_cursor.fetchall()
            ]

        is_all_time_low = False
        if current_price is not None and min_price is not None:
            is_all_time_low = current_price <= (min_price + 0.001)

        return {
            "product_key": product_key,
            "title": title,
            "product_name": title,
            "brand": brand,
            "current_price": current_price,
            "all_time_low": min_price,
            "all_time_high": max_price,
            "average_price": avg_price,
            "avg_price": avg_price,
            "record_count": record_count,
            "records_count": record_count,
            "is_all_time_low": is_all_time_low,
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
