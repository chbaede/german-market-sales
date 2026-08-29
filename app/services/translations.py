from __future__ import annotations

import re
from functools import lru_cache


PHRASES: tuple[tuple[str, str], ...] = (
    ("verschiedene sorten", "다양한 종류"),
    ("versch. sorten", "다양한 종류"),
    ("details im prospekt", "전단지 상세 정보"),
    ("preis variiert je nach groesse und variante", "크기와 종류에 따라 가격이 다름"),
    ("preis variiert je nach größe und variante", "크기와 종류에 따라 가격이 다름"),
    ("aus dem steinbackofen", "화덕에서 구운"),
    ("klasse i", "1등급"),
    ("ursprung deutschland", "독일산"),
    ("fairtrade", "공정무역"),
    ("bio baby spinat", "유기농 베이비 시금치"),
    ("caffe crema", "카페 크레마"),
    ("caffè crema", "카페 크레마"),
    ("ready to drink", "즉석 음료"),
    ("white wine", "화이트 와인"),
)

WORDS: dict[str, str] = {
    "ab": "부터",
    "aperitif": "아페리티프",
    "apfel": "사과",
    "aepfel": "사과",
    "äpfel": "사과",
    "baby": "유아",
    "baguette": "바게트",
    "banane": "바나나",
    "becher": "컵",
    "beeren": "베리",
    "binden": "생리대",
    "bio": "유기농",
    "birnen": "배",
    "blended": "블렌디드",
    "braten": "구이용 고기",
    "bratwurst": "구이용 소시지",
    "brot": "빵",
    "broetchen": "빵",
    "brötchen": "빵",
    "butter": "버터",
    "ca": "약",
    "chips": "칩",
    "cola": "콜라",
    "crema": "크레마",
    "creme": "크림",
    "delikatess": "고급",
    "deo": "데오드란트",
    "dolce": "부드러운",
    "duschgel": "샤워젤",
    "dusche": "샤워",
    "dornfelder": "도른펠더 와인",
    "duschkopf": "샤워기 헤드",
    "edelsalami": "고급 살라미",
    "eiersalat": "달걀 샐러드",
    "eis": "아이스크림",
    "eiscreme": "아이스크림",
    "espresso": "에스프레소",
    "filet": "필레",
    "fisch": "생선",
    "forelle": "송어",
    "frisch": "신선",
    "gekuehlt": "냉장",
    "gekühlt": "냉장",
    "gemuese": "채소",
    "gemüse": "채소",
    "glas": "유리병",
    "grill": "그릴",
    "gurken": "오이",
    "haehnchen": "닭고기",
    "hähnchen": "닭고기",
    "haushalt": "생활용품",
    "haar": "헤어",
    "je": "각",
    "joghurt": "요거트",
    "kaffee": "커피",
    "kalbs": "송아지",
    "kartoffeln": "감자",
    "kaese": "치즈",
    "käse": "치즈",
    "kg": "kg",
    "knusperchen": "바삭한 조각",
    "kosmetik": "화장품",
    "koerper": "바디",
    "körper": "바디",
    "kuh": "소",
    "lachs": "연어",
    "lauchzwiebeln": "쪽파",
    "liter": "리터",
    "l": "l",
    "maggi": "마기",
    "marinade": "마리네이드",
    "medaillons": "메달리온",
    "milch": "우유",
    "mozzarella": "모차렐라",
    "nudeln": "면",
    "nuss": "견과",
    "obst": "과일",
    "ofenh": "오븐",
    "ouzo": "우조",
    "packung": "포장",
    "paniert": "빵가루 입힌",
    "paprika": "파프리카",
    "pesto": "페스토",
    "pflasterspray": "상처 보호 스프레이",
    "pflege": "케어",
    "premium": "프리미엄",
    "quark": "쿼크",
    "reis": "쌀",
    "reiniger": "세제",
    "rind": "소고기",
    "rueckenbraten": "등심 구이",
    "rückenbraten": "등심 구이",
    "sahne": "크림",
    "sahnesteif": "휘핑크림 안정제",
    "salami": "살라미",
    "salat": "샐러드",
    "sauce": "소스",
    "schafmilch": "양젖",
    "schinken": "햄",
    "schlemmerfilet": "생선 필레 요리",
    "schoko": "초코",
    "schokolade": "초콜릿",
    "schweine": "돼지고기",
    "sekt": "스파클링 와인",
    "sensitiv": "민감성",
    "sensitive": "민감성",
    "shampoo": "샴푸",
    "sorte": "종류",
    "sorten": "종류",
    "spinat": "시금치",
    "stk": "개",
    "stueck": "개",
    "stück": "개",
    "suess": "달콤한",
    "süß": "달콤한",
    "tomaten": "토마토",
    "tomate": "토마토",
    "vegan": "비건",
    "wein": "와인",
    "weisskaese": "화이트 치즈",
    "weisskäse": "화이트 치즈",
    "weißkäse": "화이트 치즈",
    "whisky": "위스키",
    "windeln": "기저귀",
    "waschmittel": "세탁세제",
    "wurst": "소시지",
    "xxl": "대용량",
    "zahnpasta": "치약",
    "zahnbuerste": "칫솔",
    "zahnbürste": "칫솔",
    "zahncreme": "치약",
    "zwiebeln": "양파",
}

TOKEN_RE = re.compile(r"[A-Za-zÄÖÜäöüß]+|[0-9]+(?:[,.][0-9]+)?|[^A-Za-zÄÖÜäöüß0-9]+")


@lru_cache(maxsize=4096)
def translate_text(text: str | None) -> str:
    if not text:
        return ""

    translated = text
    for source, target in sorted(PHRASES, key=lambda pair: len(pair[0]), reverse=True):
        translated = re.sub(re.escape(source), target, translated, flags=re.IGNORECASE)

    parts: list[str] = []
    for token in TOKEN_RE.findall(translated):
        key = token.casefold()
        key_ascii = (
            key.replace("ä", "ae")
            .replace("ö", "oe")
            .replace("ü", "ue")
            .replace("ß", "ss")
        )
        parts.append(WORDS.get(key, WORDS.get(key_ascii, token)))

    result = "".join(parts)
    result = re.sub(r"\s+", " ", result).strip()
    return result if result != text else ""
