"""Conservative repairs for Hebrew prose before BlueTTS's general normalizer.

BlueTTS reads every number in the feminine counting form (שלוש ילדים, שתים עשרה ימים). A number that counts
something agrees with the word after it here: the gender comes from a list of common counted words, then from the
plural ending (ים masculine, ות feminine, with the usual exceptions). Any other number keeps BlueTTS's reading,
which is right for years, pages and numbers on their own.

Do not infer missing OCR digits or reverse ranges: those need the source page.
"""

import re

from num2words import num2words

VERSION = "hebrew-numbers-v3"
JOIN = ""
_PREFIX = re.compile(r'(?<![\w֐-׿])([ובכלמשה]{1,3})(?:[-־–\u2014‑*"״])?(?=\d)')
_GROUPED = re.compile(r"(?<![\w.,])\d{1,3}(?:,\d{3})+(?!\w|[.,]\d)")
_EN = re.compile(r"(<en>.*?</en>)", re.IGNORECASE | re.DOTALL)
_RANGE = re.compile(r"(?<![\w.\-/])([1-9]\d{0,3})\s*[-–־]\s*([1-9]\d{0,3})(?![\w.\-/]|[.,]\d)")
_NUMBER = r"\d+(?:\.\d+)?"
_PERCENT = re.compile(rf"(?<![\w.,])({_NUMBER})\s?%")
_SHEKEL = re.compile(rf"(?<![\w.,])({_NUMBER})\s?₪")
# A count: a number, or a range or choice of numbers (15 עד 20, 3 או 4, בין 15 ל-20), and the word after it.
_COUNT = re.compile(
    rf"(?<![\w.,])(?P<numbers>{_NUMBER}(?:\s+(?:עד|או)\s+{_NUMBER}|\s+[ול]{JOIN}{_NUMBER})*)"
    r"(?P<space>\s+)(?P<word>[א-ת]+)"
)
# Years: בשנת 1992, בשנות 1960, ב-1492 (a year joined to ב or מ). Their numbers are not counts.
_YEAR_BEFORE = re.compile(r"(?:^|\s)[ובלמה]{0,3}שנ(?:ת|ות|ים)\s*$")
_YEAR_PREFIX = re.compile(rf"(?:^|\s)ו?[במ]{JOIN}$")

# Words counted in the singular or with a plural that does not follow the ending (מקומות, נשים).
_MASCULINE = set("""
איש יום חודש שבוע דור עמוד פרק מטר קילומטר סנטימטר מילימטר קילוגרם קילו גרם טון ליטר שקל שקלים שקלי דולר
יורו אחוז אחוזים אלף מיליון מיליארד טריליון בני זוג מטבע
מקומות אבות לילות שולחנות חלונות קולות כוחות דורות רחובות קירות אוצרות זוגות עורות כיסאות כסאות מוסדות צבאות
מחנות שדות ארונות לוחות יינות פירות מעיינות חלומות שמות סודות חובות יתרונות חסרונות רעיונות עקרונות פתרונות
זיכרונות זכרונות חשבונות שבועות גבולות מעמדות מטבעות קורבנות קרבנות רגשות מוחות ארמונות מזבחות סולמות אותות
מאורעות מזלות גורלות
""".split())
_FEMININE = set("""
שנה פעם שעה דקה שנייה שניה לירה ליברה אגורה מעלה נפש קלוריה
נשים שנים ערים מילים ביצים אבנים פעמים רגליים ידיים עיניים אוזניים שיניים כנפיים ציפורים יונים דבורים נמלים עזים
תאנים שעורים חיטים דרכים נעליים ברכיים שפתיים ציפורניים קרניים
""".split())

# Units and abbreviations that BlueTTS would read letter by letter once it drops their quote mark (ק"ג → קג).
_UNITS = {
    'ק"מ': "קילומטר", 'ס"מ': "סנטימטר", 'מ"מ': "מילימטר", 'ק"ג': "קילוגרם", 'מ"ר': "מטר רבוע",
    'סמ"ק': "סנטימטר מעוקב", 'קמ"ש': "קילומטר לשעה", 'ש"ח': "שקלים",
}
_ABBREVIATIONS = {
    'ד"ר': "דוקטור", "פרופ'": "פרופסור", 'לפנה"ס': "לפני הספירה", 'לסה"נ': "לספירת הנוצרים",
    'ארה"ב': "ארצות הברית", 'ע"י': "על ידי", 'עפ"י': "על פי", "וכו'": "וכולי", "עמ'": "עמוד", "מס'": "מספר",
    'סה"כ': "סך הכל", 'בסה"כ': "בסך הכל", 'בד"כ': "בדרך כלל", 'כ"כ': "כל כך", 'ז"א': "זאת אומרת",
    'אא"כ': "אלא אם כן", 'יו"ר': "יושב ראש", 'עו"ד': "עורך דין", 'אחה"צ': "אחר הצהריים", 'ת"א': "תל אביב",
}


def _choices(words) -> str:
    return "|".join(re.escape(w).replace('"', '["״]').replace("'", "['׳]") for w in sorted(words, key=len, reverse=True))


_UNIT = re.compile(rf"(?<=\d)(\s*)({_choices(_UNITS)})(?![\w֐-׿])")
_ABBREVIATION = re.compile(rf"(?<![\w֐-׿])((?:ו?(?:ש|כש|מש)?[כלבמ]?ה?)?)({_choices(_ABBREVIATIONS)})(?![\w֐-׿])")
# OCR reads a hyphen between two words as a quote mark or a star (לקטים"ציידים, אפרו*אסיה), and BlueTTS joins
# hyphenated words into one. Both become two words, except after a prefix (ש"האדם is ש + a quotation: one word).
_HYPHENATED = re.compile(r"(?<![א-ת])([א-ת]{2,})[-־\"״*]([א-ת]{2,})")
_PREFIXES = re.compile(r"ו?(?:ש|כש|מש)?[כלבמ]?ה?")


def repair_words(text: str) -> str:
    """Hebrew units, abbreviations and OCR hyphens spelled out as the narrator says them."""

    def repair(part: str) -> str:
        part = _UNIT.sub(lambda m: m[1] + _UNITS[_plain(m[2])], part)
        part = _ABBREVIATION.sub(lambda m: m[1] + _ABBREVIATIONS[_plain(m[2])], part)
        # Twice: a match ends on the word the next one starts with (יש-אריה-ליד-הנהר).
        for _ in range(2):
            part = _HYPHENATED.sub(lambda m: m[0] if _PREFIXES.fullmatch(m[1]) else f"{m[1]} {m[2]}", part)
        return part

    return "".join(p if _EN.fullmatch(p) else repair(p) for p in _EN.split(text))


def _plain(abbreviation: str) -> str:
    return abbreviation.replace("״", '"').replace("׳", "'")


def gender(word: str) -> str | None:
    """"m" or "f" for a word that is counted, None for any other word (its number keeps BlueTTS's reading)."""
    for w in (word, word[1:] if word.startswith("ה") and len(word) > 4 else None):
        if w in _MASCULINE:
            return "m"
        if w in _FEMININE:
            return "f"
    if len(word) >= 4 and word.endswith("ות"):
        return "f"
    if len(word) >= 4 and word.endswith("ים"):
        return "m"
    return None


def prepare_numbers(text: str) -> str:
    def prepare(part: str) -> str:
        # OCR sometimes reads the hyphen as * or a quote. Only repair that
        # between a standalone Hebrew prefix and digits; *6700 stays a code.
        part = _PREFIX.sub(lambda m: m[1] + JOIN, part)
        # Resolve grouping even immediately before sentence punctuation.
        part = _GROUPED.sub(lambda m: m[0].replace(",", ""), part)
        # Keep the printed order; a descending OCR range cannot safely be
        # reversed without checking the page. Exclude phone/date/code forms.
        part = _RANGE.sub(r"\1 עד \2", part)
        # אחוז is masculine (עשרה אחוזים); BlueTTS would say עשר אחוז.
        part = _PERCENT.sub(lambda m: m[1] + (" אחוזים" if m[1].isdigit() and 2 <= int(m[1]) <= 10 else " אחוז"), part)
        part = _SHEKEL.sub(r"\1 שקלים", part)
        return _COUNT.sub(count, part)

    def count(m: re.Match) -> str:
        noun = gender(m["word"])
        before = m.string[:m.start()]
        if noun is None or _YEAR_BEFORE.search(before) or (_YEAR_PREFIX.search(before) and re.fullmatch(r"1\d{3}|20\d\d", m["numbers"])):
            return m[0]
        numbers, word = m["numbers"], m["word"]
        if numbers == "1":  # one comes after the noun: שנה אחת
            return word + m["space"] + num2words(1, lang="he", gender=noun)
        if word == "אלף" and numbers.isdigit() and 2 <= int(numbers) <= 10:  # שלושת אלפים, not שלוש אלף
            return num2words(int(numbers) * 1000, lang="he")
        last = list(re.finditer(_NUMBER, numbers))[-1]

        def say(n: re.Match) -> str:
            if "." in n[0]:  # decimals are said in the counting form: שבע נקודה חמש קילומטרים
                return num2words(n[0], lang="he", gender="f")
            return num2words(n[0], lang="he", gender=noun, construct=n[0] == "2" and n.start() == last.start())

        return re.sub(_NUMBER, say, numbers) + m["space"] + word

    return "".join(p if _EN.fullmatch(p) else prepare(p) for p in _EN.split(text))
