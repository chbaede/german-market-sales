from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.models import Offer
from app.services.translations import translate_text


KOREAN_PRODUCT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "파": ("lauchzwiebeln", "frühlingszwiebeln", "bundzwiebeln"),
    "쪽파": ("lauchzwiebeln", "frühlingszwiebeln", "bundzwiebeln"),
    "대파": ("lauch", "porree", "frühlingszwiebeln"),
    "당근": ("möhren", "möhre", "karotten", "karotte"),
    "양파": ("zwiebeln", "zwiebel"),
    "감자": ("kartoffeln", "kartoffel"),
    "고구마": ("süßkartoffeln", "süsskartoffeln", "süßkartoffel"),
    "마늘": ("knoblauch",),
    "생강": ("ingwer",),
    "버섯": ("champignons", "champignon", "pilze"),
    "양송이": ("champignons", "champignon"),
    "토마토": ("tomaten", "tomate"),
    "방울토마토": ("cherrytomaten", "rispentomaten", "tomaten"),
    "오이": ("gurke", "gurken"),
    "상추": ("salat",),
    "샐러드": ("salat",),
    "시금치": ("spinat",),
    "파프리카": ("paprika",),
    "브로콜리": ("brokkoli",),
    "사과": ("äpfel", "apfel"),
    "바나나": ("bananen", "banane"),
    "딸기": ("erdbeeren", "erdbeere"),
    "블루베리": ("heidelbeeren", "blaubeeren"),
    "포도": ("trauben",),
    "오렌지": ("orangen", "orange"),
    "레몬": ("zitronen", "zitrone"),
    "라임": ("limetten", "limette"),
    "우유": ("milch",),
    "버터": ("butter",),
    "치즈": ("käse", "kaese"),
    "모짜렐라": ("mozzarella",),
    "요거트": ("joghurt", "yoghurt"),
    "계란": ("eier", "ei"),
    "달걀": ("eier", "ei"),
    "크림": ("sahne", "creme"),
    "닭고기": ("hähnchen", "haehnchen"),
    "닭": ("hähnchen", "haehnchen"),
    "돼지고기": ("schwein", "schweine"),
    "소고기": ("rind", "rinder"),
    "소세지": ("wurst", "würstchen", "bratwurst"),
    "소시지": ("wurst", "würstchen", "bratwurst"),
    "햄": ("schinken",),
    "연어": ("lachs",),
    "생선": ("fisch",),
    "새우": ("garnelen", "shrimp"),
    "빵": ("brot", "brötchen", "toast"),
    "식빵": ("toast", "brot"),
    "파스타": ("pasta", "nudeln"),
    "면": ("nudeln",),
    "쌀": ("reis",),
    "밥": ("reis",),
    "밀가루": ("mehl",),
    "설탕": ("zucker",),
    "소금": ("salz",),
    "기름": ("öl", "oel"),
    "올리브유": ("olivenöl", "olivenoel"),
    "커피": ("kaffee", "caffè", "cafe"),
    "차": ("tee",),
    "물": ("wasser",),
    "주스": ("saft",),
    "콜라": ("cola",),
    "맥주": ("bier",),
    "와인": ("wein",),
    "세제": ("waschmittel", "reiniger", "spülmittel"),
    "주방세제": ("spülmittel", "spuelmittel"),
    "휴지": ("toilettenpapier", "papier"),
    "키친타월": ("küchenrolle", "küchentücher", "papier"),
    "물티슈": ("feuchttücher", "feuchttuecher"),
    "샴푸": ("shampoo",),
    "샤워젤": ("duschgel", "dusche"),
    "바디워시": ("duschgel", "dusche"),
    "치약": ("zahnpasta", "zahncreme"),
    "칫솔": ("zahnbürste", "zahnbuerste"),
    "비누": ("seife",),
    "생리대": ("binden", "tampons"),
    "로션": ("lotion", "bodylotion"),
    "선크림": ("sonnencreme", "sonnenschutz"),
    "데오드란트": ("deo", "deodorant"),
    "화장품": ("kosmetik", "make-up", "makeup"),
    "화장솜": ("wattepads", "watte"),
    "기저귀": ("windeln", "pampers"),
    "면도기": ("rasierer", "nassrasierer"),
}
KOREAN_PRODUCT_CATEGORIES: dict[str, tuple[str, ...]] = {
    "파": ("produce",),
    "쪽파": ("produce",),
    "대파": ("produce",),
    "당근": ("produce",),
    "양파": ("produce",),
    "감자": ("produce",),
    "고구마": ("produce",),
    "마늘": ("produce",),
    "생강": ("produce",),
    "버섯": ("produce",),
    "양송이": ("produce",),
    "토마토": ("produce",),
    "방울토마토": ("produce",),
    "오이": ("produce",),
    "상추": ("produce",),
    "샐러드": ("produce",),
    "시금치": ("produce",),
    "파프리카": ("produce",),
    "브로콜리": ("produce",),
    "사과": ("produce",),
    "바나나": ("produce",),
    "딸기": ("produce",),
    "블루베리": ("produce",),
    "포도": ("produce",),
    "오렌지": ("produce",),
    "레몬": ("produce",),
    "라임": ("produce",),
    "우유": ("dairy",),
    "버터": ("dairy",),
    "치즈": ("dairy",),
    "모짜렐라": ("dairy",),
    "요거트": ("dairy",),
    "계란": ("dairy",),
    "달걀": ("dairy",),
    "크림": ("dairy",),
    "닭고기": ("meat_fish",),
    "닭": ("meat_fish",),
    "돼지고기": ("meat_fish",),
    "소고기": ("meat_fish",),
    "소세지": ("meat_fish",),
    "소시지": ("meat_fish",),
    "햄": ("meat_fish",),
    "연어": ("meat_fish",),
    "생선": ("meat_fish",),
    "새우": ("meat_fish",),
    "빵": ("bakery",),
    "식빵": ("bakery",),
    "파스타": ("pantry",),
    "면": ("pantry",),
    "쌀": ("pantry",),
    "밥": ("pantry",),
    "밀가루": ("pantry",),
    "설탕": ("pantry",),
    "소금": ("pantry",),
    "기름": ("pantry",),
    "올리브유": ("pantry",),
    "커피": ("pantry",),
    "차": ("pantry",),
    "물": ("drinks",),
    "주스": ("drinks",),
    "콜라": ("drinks",),
    "맥주": ("drinks",),
    "와인": ("drinks",),
    "세제": ("household",),
    "주방세제": ("household",),
    "휴지": ("household",),
    "키친타월": ("household",),
    "물티슈": ("household", "baby"),
    "샴푸": ("personal_care",),
    "샤워젤": ("personal_care",),
    "바디워시": ("personal_care",),
    "치약": ("personal_care",),
    "칫솔": ("personal_care",),
    "비누": ("personal_care",),
    "생리대": ("personal_care",),
    "로션": ("personal_care",),
    "선크림": ("personal_care",),
    "데오드란트": ("personal_care",),
    "화장품": ("personal_care",),
    "화장솜": ("personal_care",),
    "기저귀": ("baby",),
    "면도기": ("personal_care",),
}

QUANTITY_RE = re.compile(
    r"(?P<amount>\d+(?:[,.]\d+)?)\s*(?P<unit>kg|킬로|킬로그램|g|그램|개|봉|팩|묶음|병|통|리터|l|L)?"
)


@dataclass(slots=True)
class BasketItem:
    raw: str
    name: str
    amount: float | None
    unit: str | None
    keywords: tuple[str, ...]
    categories: tuple[str, ...] = ()


@dataclass(slots=True)
class BasketCandidate:
    offer: Offer
    score: int
    matched_keywords: tuple[str, ...]
    estimated_total: float | None
    estimate_note: str


@dataclass(slots=True)
class BasketRecommendation:
    item: BasketItem
    candidates: list[BasketCandidate]


def recommend_basket_items(text: str | None, offers: list[Offer], limit_per_item: int = 3) -> list[BasketRecommendation]:
    items = parse_basket_items(text)
    return [_recommend_for_item(item, offers, limit_per_item) for item in items]


def parse_basket_items(text: str | None) -> list[BasketItem]:
    if not text:
        return []

    parts = [part.strip() for part in re.split(r"[,，;\n]+", text) if part.strip()]
    return [_parse_item(part) for part in parts]


def _parse_item(raw: str) -> BasketItem:
    amount: float | None = None
    unit: str | None = None
    name = raw
    match = QUANTITY_RE.search(raw)
    if match:
        amount = float(match.group("amount").replace(",", "."))
        unit = _normalize_unit(match.group("unit"))
        name = (raw[: match.start()] + raw[match.end() :]).strip()
    name = re.sub(r"\s+", " ", name).strip() or raw.strip()
    keywords = _keywords_for_name(name)
    categories = _categories_for_name(name)
    return BasketItem(raw=raw.strip(), name=name, amount=amount, unit=unit, keywords=keywords, categories=categories)


def _recommend_for_item(item: BasketItem, offers: list[Offer], limit_per_item: int) -> BasketRecommendation:
    candidates: list[BasketCandidate] = []
    for offer in offers:
        if item.categories and offer.category not in item.categories:
            continue
        score, matched = _match_score(item, offer)
        if score <= 0:
            continue
        estimated_total, estimate_note = _estimate_price(item, offer)
        candidates.append(
            BasketCandidate(
                offer=offer,
                score=score,
                matched_keywords=tuple(matched),
                estimated_total=estimated_total,
                estimate_note=estimate_note,
            )
        )

    candidates = sorted(
        candidates,
        key=lambda candidate: (
            -candidate.score,
            candidate.estimated_total is None,
            candidate.estimated_total or candidate.offer.price or 999999,
            -(candidate.offer.discount_percent or 0),
            candidate.offer.retailer.casefold(),
        ),
    )[:limit_per_item]
    return BasketRecommendation(item=item, candidates=candidates)


def _match_score(item: BasketItem, offer: Offer) -> tuple[int, list[str]]:
    title = _normalize(offer.title)
    description = _normalize(offer.description)
    translated = _normalize(" ".join((translate_text(offer.title), translate_text(offer.description))))
    haystack = " ".join(
        _normalize(part)
        for part in (offer.title, offer.brand or "", offer.description, translated)
        if part
    )

    score = 0
    matched: list[str] = []
    for keyword in item.keywords:
        normalized = _normalize(keyword)
        if not normalized:
            continue
        if normalized in title:
            score += 8
            matched.append(keyword)
        elif normalized in description:
            score += 4
            matched.append(keyword)
        elif normalized in translated or normalized in haystack:
            score += 3
            matched.append(keyword)

    item_name = _normalize(item.name)
    if len(item.name.strip()) >= 2 and item_name in translated:
        score += 5
    return score, list(dict.fromkeys(matched))


def _estimate_price(item: BasketItem, offer: Offer) -> tuple[float | None, str]:
    if offer.price is None and offer.unit_price is None:
        return None, "가격 정보 없음"

    if item.amount and item.unit in {"kg", "g"} and offer.unit_price is not None and offer.unit:
        amount_kg = item.amount if item.unit == "kg" else item.amount / 1000
        if "kg" in offer.unit.casefold():
            return round(offer.unit_price * amount_kg, 2), "단위가격 기준 예상"

    if item.amount and item.unit in {"개", "봉", "팩", "묶음", "병", "통", "l"} and offer.price is not None:
        return round(offer.price * item.amount, 2), "수량 기준 단순 예상"

    if offer.price is not None:
        return offer.price, "행사가 기준"
    return offer.unit_price, "단위가격 기준"


def _keywords_for_name(name: str) -> tuple[str, ...]:
    normalized = re.sub(r"\s+", "", name.casefold())
    keywords: list[str] = []
    for korean, german_keywords in KOREAN_PRODUCT_KEYWORDS.items():
        if korean in normalized:
            keywords.extend(german_keywords)
    if not keywords:
        keywords.append(name)
    return tuple(dict.fromkeys(keywords))


def _categories_for_name(name: str) -> tuple[str, ...]:
    normalized = re.sub(r"\s+", "", name.casefold())
    categories: list[str] = []
    for korean, target_categories in KOREAN_PRODUCT_CATEGORIES.items():
        if korean in normalized:
            categories.extend(target_categories)
    return tuple(dict.fromkeys(categories))


def _normalize_unit(unit: str | None) -> str | None:
    if not unit:
        return None
    unit = unit.casefold()
    return {
        "킬로": "kg",
        "킬로그램": "kg",
        "그램": "g",
        "리터": "l",
    }.get(unit, unit)


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.replace("ß", "ss")
    return re.sub(r"\s+", " ", value).strip()
