"""Persian normalization and tokenization.

Folding Arabic letter variants and digits makes matching stable; ZWNJ is kept
for display but dropped from matching keys. The corpus stores the original
text separately, so normalization never destroys information.
"""
from __future__ import annotations

import re

ZWNJ = "\u200c"

_FOLD = {
    "ي": "ی", "ك": "ک", "ة": "ه", "أ": "ا", "إ": "ا",
    "ؤ": "و", "ٱ": "ا", "ۀ": "ه", "ئ": "ی",
}
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_MARKS = re.compile(r"[\u064B-\u0652\u0670\u061C\u200d\u200e\u200f\u202a-\u202e]")
_SPACE = re.compile(r"\s+")
_TOKEN = re.compile(r"[^\W_]+(?:\u200c[^\W_]+)*", re.UNICODE)
_SENTENCE = re.compile(r"[.!؟?\n]+")
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def normalize(text: str, *, keep_zwnj: bool = False) -> str:
    folded = "".join(_FOLD.get(char, char) for char in str(text or ""))
    value = folded.translate(_DIGITS)
    value = _MARKS.sub("", value)
    if not keep_zwnj:
        value = value.replace(ZWNJ, "")
    return _SPACE.sub(" ", value).strip().lower()


def tokens(text: str) -> list[str]:
    """Matching keys in order; a ZWNJ word stays a single token."""
    surface = normalize(text, keep_zwnj=True)
    return [normalize(match.group(0)) for match in _TOKEN.finditer(surface)]


def sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE.split(str(text or "")) if part.strip()]


def persian_ratio(text: str) -> float:
    letters = _LETTER.findall(str(text or ""))
    if not letters:
        return 0.0
    persian = sum(1 for char in letters if "\u0600" <= char <= "\u06FF")
    return persian / len(letters)


def quality(text: str) -> float:
    """Heuristic 0..1. Low scores are stored but held back from confident use."""
    value = normalize(text)
    if len(value) < 3:
        return 0.1
    letters = len(_LETTER.findall(value))
    letter_ratio = letters / max(1, len(value))
    variety = len(set(value)) / max(1, len(value))
    score = (0.3 * min(1.0, len(value) / 60)
             + 0.3 * letter_ratio
             + 0.4 * variety)
    return round(max(0.05, min(1.0, score)), 3)