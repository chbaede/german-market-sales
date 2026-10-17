from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

BERLIN_TZ = ZoneInfo("Europe/Berlin")


def format_eur_value(value: float | int | None) -> str | None:
    if value is None:
        return None
    return f"{value:,.2f} EUR".replace(",", "X").replace(".", ",").replace("X", ".")


def format_eur_display(value: float | int | None) -> str:
    return format_eur_value(value) or "-"


def format_percent_value(value: float | int | None) -> str | None:
    if value is None or value <= 0:
        return None
    return f"-{round(value)}%"


def format_percent_display(value: float | int | None) -> str:
    return format_percent_value(value) or ""


def format_amount_value(value: float | int | None) -> str:
    if value is None:
        return ""
    if float(value).is_integer():
        return str(int(value))
    return str(value).replace(".", ",")


def format_amount_with_unit(value: float | None, unit: str | None) -> str:
    amount = format_amount_value(value)
    if not amount:
        return ""
    return " ".join(part for part in (amount, unit or "") if part)


@lru_cache(maxsize=4096)
def parse_offer_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(BERLIN_TZ)
    return timestamp.date()


@lru_cache(maxsize=4096)
def format_date_de(value: str | None) -> str:
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
