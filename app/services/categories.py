from __future__ import annotations

import re


KEYWORDS: dict[str, tuple[str, ...]] = {
    "produce": (
        "apfel",
        "äpfel",
        "banane",
        "beeren",
        "birne",
        "champignon",
        "gurke",
        "kartoffel",
        "lauch",
        "möhren",
        "obst",
        "paprika",
        "salat",
        "spargel",
        "tomate",
        "trauben",
        "zwiebel",
    ),
    "dairy": (
        "butter",
        "joghurt",
        "käse",
        "milch",
        "mozzarella",
        "quark",
        "sahne",
        "skyr",
        "weisskäse",
        "weißkäse",
    ),
    "meat_fish": (
        "braten",
        "filet",
        "fisch",
        "forelle",
        "hähnchen",
        "kalb",
        "lachs",
        "mett",
        "rind",
        "salami",
        "schinken",
        "schwein",
        "steak",
        "wurst",
    ),
    "bakery": (
        "back",
        "baguette",
        "brötchen",
        "brot",
        "croissant",
        "kuchen",
        "toast",
    ),
    "pantry": (
        "bohnen",
        "cerealien",
        "chips",
        "eis",
        "kaffee",
        "konserve",
        "mehl",
        "müsli",
        "nudeln",
        "öl",
        "pasta",
        "pesto",
        "reis",
        "sauce",
        "schokolade",
        "suppe",
        "tee",
    ),
    "drinks": (
        "bier",
        "cola",
        "drink",
        "getränk",
        "limonade",
        "saft",
        "sekt",
        "wasser",
        "wein",
    ),
    "household": (
        "batterie",
        "filterbeutel",
        "folie",
        "gefrierbeutel",
        "küche",
        "müllbeutel",
        "papier",
        "reiniger",
        "staubfilter",
        "spül",
        "toiletten",
        "wasch",
    ),
    "personal_care": (
        "binden",
        "concealer",
        "creme",
        "deo",
        "dusch",
        "foundation",
        "kosmetik",
        "lippen",
        "make-up",
        "mascara",
        "nagel",
        "parfum",
        "pflege",
        "pflaster",
        "rasur",
        "rasierer",
        "shampoo",
        "seife",
        "sonnenmilch",
        "tape",
        "zahnpasta",
    ),
    "baby": ("baby", "pampers", "windeln"),
    "nonfood": (
        "anzug",
        "bluse",
        "deko",
        "dekoration",
        "duftlicht",
        "geschirr",
        "hemd",
        "hose",
        "handschuh",
        "jacke",
        "kerze",
        "kleid",
        "kleidung",
        "kunststoff",
        "leggings",
        "mantel",
        "mütze",
        "pullover",
        "regenjacke",
        "schal",
        "schlafanzug",
        "schuhe",
        "shirt",
        "socken",
        "sneaker",
        "strick",
        "teller",
        "textil",
        "wäsche",
    ),
}

PRIORITY_CATEGORIES = ("nonfood", "personal_care", "baby")
STRICT_KEYWORDS = {"eis", "mett", "obst", "öl", "reis", "tee"}


def categorize_offer(title: str, brand: str | None = None, description: str | None = None) -> str:
    haystack = " ".join(part for part in (title, brand or "", description or "") if part).casefold()
    for category in PRIORITY_CATEGORIES:
        if _matches_any(haystack, KEYWORDS[category]):
            return category
    for category, words in KEYWORDS.items():
        if category in PRIORITY_CATEGORIES:
            continue
        if _matches_any(haystack, words):
            return category
    return "other"


def _matches_any(haystack: str, words: tuple[str, ...]) -> bool:
    return any(_matches_keyword(haystack, word) for word in words)


def _matches_keyword(haystack: str, word: str) -> bool:
    if word in STRICT_KEYWORDS or len(word) <= 3:
        return re.search(rf"(?<![a-zäöüß]){re.escape(word)}(?![a-zäöüß])", haystack) is not None
    return word in haystack
