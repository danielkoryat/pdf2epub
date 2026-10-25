import pytest

from pdf2epub_voice.engines.blue import spell_out
from pdf2epub_voice.numbers import prepare_numbers


@pytest.mark.parametrize(("source", "expected"), [
    ("לפני כ*13.5 מיליארד שנים", "לפני כשלוש עשרה נקודה חמש מיליארד שנים"),  # decimals in the counting form
    ("לפני כ*70,000 שנה", "לפני כשבעים אלף שנה"),
    ('לפני כ"70,000 שנה', "לפני כשבעים אלף שנה"),
    ("לפני כ400 אלף שנה", "לפני כארבע מאות אלף שנה"),
    ("ב־1492 הגיע קולומבוס", "באלף ארבע מאות תשעים ושתיים הגיע קולומבוס"),
    ("היו 70,000.", "היו שבעים אלף."),
    ("היו 1,225, ואז הלכו.", "היו אלף מאתיים עשרים וחמש, ואז הלכו."),
    ("6 מיליון שנה", "שישה מיליון שנה"),
    ("2 מיליון שנה", "שני מיליון שנה"),
    ("3 ילדים", "שלושה ילדים"),
    ("2 נשים", "שתי נשים"),
    ("3 שנים", "שלוש שנים"),
    ("המאה ה-15", "המאה החמש עשרה"),
    ("לפני כ*70-60 אלף שנה", "לפני כשבעים עד שישים אלף שנה"),
    ("בשנים 1914–1918", "בשנים אלף תשע מאות וארבע עשרה עד אלף תשע מאות ושמונה עשרה"),
    # The gender comes from the counted word: listed words, then the plural ending.
    ("לטיול של 12 ימים", "לטיול של שנים עשר ימים"),
    ("עברו בין 15 ל-20 יישובים", "עברו בין חמישה עשר לעשרים יישובים"),
    ("ועוד 85 שקלים", "ועוד שמונים וחמישה שקלים"),
    ("2 או 3 ילדים", "שניים או שלושה ילדים"),
    ("בין 3 ל-4 שבועות", "בין שלושה לארבעה שבועות"),
    ("8 מלחמות", "שמונה מלחמות"),
    ("3 פעמים", "שלוש פעמים"),
    ("הלכו 7.5 קילומטרים", "הלכו שבע נקודה חמש קילומטרים"),
    ("הנחה של 10%", "הנחה של עשרה אחוזים"),
    ("קיבלו 50% הנחה", "קיבלו חמישים אחוז הנחה"),
    ("1 שנה", "שנה אחת"),
    ("לפני 3 אלף שנה", "לפני שלושת אלפים שנה"),
    ("השוקל 55 ק\"ג", "השוקל חמישים וחמישה קילוגרם"),
    ("100 ₪", "מאה שקלים"),
    # Years and numbers that count nothing keep BlueTTS's reading.
    ("בשנת 1992 אנשים רבים", "בשנת אלף תשע מאות תשעים ושתיים אנשים רבים"),
    ("ב-1492 ספינות", "באלף ארבע מאות תשעים ושתיים ספינות"),
    ("ב-1492 הגיע קולומבוס", "באלף ארבע מאות תשעים ושתיים הגיע קולומבוס"),
])
def test_numbers_in_hebrew_prose(source, expected):
    assert spell_out(source) == expected


@pytest.mark.parametrize(("source", "expected"), [
    ('לקטים"ציידים', "לקטים ציידים"),  # OCR reads the hyphen as a quote mark
    ('בו"זמנית', "בו זמנית"),
    ("אפרו*אסיה", "אפרו אסיה"),
    ("לקטים-ציידים", "לקטים ציידים"),  # BlueTTS would join them into one word
    ("יש-אריה-ליד-הנהר", "יש אריה ליד הנהר"),
    ('ש"האדם', "שהאדם"),  # a prefix before a quotation stays on its word
    ('וה"אדם', "והאדם"),
    ('הדנ"א והתנ"ך', "הדנא והתנך"),  # acronyms said as words keep BlueTTS's reading
    ('ד"ר סמית', "דוקטור סמית"),
    ('לפנה"ס', "לפני הספירה"),
    ('בארה"ב', "בארצות הברית"),
    ("וכו'", "וכולי"),
])
def test_words_said_as_the_narrator_says_them(source, expected):
    assert spell_out(source) == expected


def test_english_spans_and_unknown_ocr_values_are_not_guessed():
    assert prepare_numbers('<en>6 million in 1492</en>') == '<en>6 million in 1492</en>'
    assert spell_out("לפני 0 שנה") == "לפני אפס שנה"
    assert "ארבע מאות ושבע" in spell_out("לפני כ407 אלף שנה")


def test_phone_date_time_and_code_paths_still_work():
    assert "כוכבית" in spell_out("חייגו *6700")
    assert "【" in spell_out("חייגו 03-5551234")
    assert "לחמישי" in spell_out("12/05/2024")
    assert "שמונה" in spell_out("בשעה 08:15")
