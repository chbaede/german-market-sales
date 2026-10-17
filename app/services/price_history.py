from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
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


def normalize_product_key(title: str, brand: str | None = None) -> str:
    combined = f"{brand or ''} {title}".strip()
    text = unicodedata.normalize("NFKD", combined)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("ß", "ss").casefold()

    text = QUANTITY_REGEX.sub(" ", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    words = [word for word in text.split() if len(word) >= 2 and word not in STOP_WORDS]
    if not words:
        return re.sub(r"\s+", "-", text.strip()) or "item"
    return "-".join(sorted(set(words[:5])))


class PriceHistoryService:
    def __init__(self, cache_dir: Path, db_name: str = "price_history.db") -> None:
        self.cache_dir = cache_dir
        self.db_path = cache_dir / db_name
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

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
        timestamp = recorded_at or datetime.now(UTC).isoformat(timespec="seconds")
        date_prefix = timestamp.split("T", 1)[0]
        inserted = 0

        with self._get_connection() as conn:
            for offer in offers:
                if offer.price is None:
                    continue

                product_key = normalize_product_key(offer.title, offer.brand)

                # Avoid duplicate insertion for identical product, retailer, price on the same day
                cursor = conn.execute(
                    """
                    SELECT 1 FROM price_history
                    WHERE product_key = ? AND retailer_slug = ? AND price = ? AND recorded_at LIKE ?
                    LIMIT 1
                    """,
                    (product_key, offer.retailer_slug, offer.price, f"{date_prefix}%"),
                )
                if cursor.fetchone():
                    continue

                conn.execute(
                    """
                    INSERT INTO price_history (
                        product_key, title, brand, category, retailer_slug, retailer_name,
                        price, old_price, unit_price, unit, discount_percent, recorded_at,
                        valid_from, valid_to, source_url, zip_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
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
                    ),
                )
                inserted += 1

            conn.commit()
        return inserted

    def get_all_time_low_map(self) -> dict[str, float]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT product_key, MIN(price) as min_price
                FROM price_history
                WHERE price IS NOT NULL AND price > 0
                GROUP BY product_key
                """
            )
            return {row["product_key"]: float(row["min_price"]) for row in cursor.fetchall()}

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
                    recorded_at, price, old_price, retailer_name, retailer_slug, source_url, unit_price, unit
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
            "record_count": record_count,
            "records_count": record_count,
            "is_all_time_low": is_all_time_low,
            "history": history,
        }

    def compare_across_stores(self, query: str, offers: list[Offer]) -> dict:
        query_key = query.casefold().strip()
        tokens = [t for t in re.split(r"\s+", query_key) if len(t) >= 2]
        if not tokens:
            tokens = [query_key]

        matching: list[Offer] = []
        for offer in offers:
            haystack = f"{offer.title} {offer.brand or ''} {offer.description}".casefold()
            if all(token in haystack for token in tokens):
                matching.append(offer)

        # Group by retailer
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
        savings_pct = round((max_savings / max_price) * 100, 1) if (max_price and max_price > 0 and max_savings > 0) else 0.0

        stores = []
        for retailer_name, offers_list in by_retailer.items():
            cheapest_in_store = min(offers_list, key=lambda o: (o["price"] is None, o["price"] or 999999))
            stores.append(cheapest_in_store)
        stores.sort(key=lambda s: (s["price"] is None, s["price"] or 999999))

        cheapest_dict = (
            {"retailer": cheapest_offer.retailer, "price": cheapest_offer.price, "title": cheapest_offer.title}
            if cheapest_offer else None
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

    def seed_initial_history(self) -> int:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) as count FROM price_history")
            count = cursor.fetchone()["count"]
            if count > 50:
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
                past_offers: list[Offer] = []
                for offer in offers:
                    if offer.old_price and offer.old_price > 0:
                        past_offers.append(
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
                            )
                        )
                if past_offers:
                    imported += self.record_offers(past_offers, zip_code=zip_code, recorded_at=past_week)
            except Exception:
                continue

        return imported
