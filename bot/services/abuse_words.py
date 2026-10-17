"""Abusive-language filter for chat messages and profile text (pure, no I/O).

It looks for WHOLE WORDS from a reviewed list, in English, Hindi written in Latin letters
("Hinglish") and Devanagari. Whole words only: "class", "assistant", "chutney", "Scunthorpe" or
"shiitake" are never touched, because a word is matched as a complete token, never as a piece of
a longer one. Light evasion is undone first: upper/lower case, repeated letters ("fuuuck"),
symbols and digits used as letters ("sh1t", "$hit", "f@ck"), letters spread out ("f u c k",
"f.u.c.k") and invisible characters. Ambiguous words are deliberately left out of the list
("chod" also means "leave", "kutta" also means "dog", "mc" / "bc" are common abbreviations):
a missed insult can be reported, a wrongly blocked sentence just annoys.

The text is never stored or logged here.
"""

from __future__ import annotations

import re
import unicodedata

from .content_rules import normalize

# English: a stem plus the usual endings (fucking, fucked, bitches, shitty, ...)
_ENGLISH = (
    "fuck", "fck", "fuk", "fack", "motherfucker", "shit", "sht", "bitch", "btch", "bastard",
    "asshole",
    "ass", "cunt", "slut", "whore", "nigger", "nigga", "faggot", "retard", "wanker", "dipshit",
    "dumbass", "jackass", "prick",
)  # fmt: skip
_ENGLISH_ENDINGS = r"(?:s|es|ed|er|ers|ing|in|y)?"

# Hindi in Latin letters (several common spellings of each)
_HINGLISH = (
    "madarchod", "maderchod", "madarchodd", "mcchod", "behenchod", "bhenchod", "bhaenchod",
    "bhosdike", "bhosadike", "bhosdi", "bhosda", "bhosdika", "bsdk", "chutiya", "chutiye",
    "chutiyapa", "chut", "lund", "lauda", "laude", "lode", "loda", "randi", "raand", "harami",
    "haramzada", "haramzade", "haramkhor", "gandu", "gaandu", "gaand", "bhadwa", "bhadwe",
    "bhadwi", "kamina", "kameena", "kamine",
)  # fmt: skip

# Hindi in Devanagari
_DEVANAGARI = (
    "मादरचोद", "मादरचोदो", "बहनचोद", "भोसड़ीके", "भोसडीके", "चूतिया", "चुतिया", "चूतिये", "लंड",
    "गांडू", "गांड", "रंडी", "हरामी", "हरामज़ादा", "हरामजादा", "हरामखोर", "कमीना", "कमीने",
    "भड़वा", "भड़वे",
)  # fmt: skip


def _stem(word: str) -> str:
    """The word with each letter allowed to repeat ("fuuuck" still matches "fuck")."""
    return "".join(re.escape(c) + "+" for c in normalize(word))


def _build() -> re.Pattern[str]:
    parts = [_stem(w) + _ENGLISH_ENDINGS for w in _ENGLISH]
    parts += [_stem(w) for w in (*_HINGLISH, *_DEVANAGARI)]
    return re.compile(r"(?:" + "|".join(parts) + r")")


_WORD = _build()

# digits and symbols people use for letters
_LEET = str.maketrans(
    {
        "@": "a",
        "$": "s",
        "!": "i",
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "*": None,
        "+": "t",
    }  # fmt: skip
)


def _tokens(text: str) -> list[str]:
    """Runs of letters, combining marks and digits (so a Devanagari word stays in one piece)."""
    out: list[str] = []
    cur: list[str] = []
    for ch in text:
        if unicodedata.category(ch)[0] in "LMN":
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def _spread_letters(tokens: list[str]) -> list[str]:
    """ "f u c k" / "f.u.c.k": runs of 3 or more single letters, joined into one word."""
    joined: list[str] = []
    run: list[str] = []
    for tok in [*tokens, ""]:
        if len(tok) == 1 and tok.isalpha():
            run.append(tok)
            continue
        if len(run) >= 3:
            joined.append("".join(run))
        run = []
    return joined


def has_abuse(text: str) -> bool:
    """True when the text contains a listed abusive word (see the module docstring)."""
    n = normalize(text)
    for variant in (n, n.translate(_LEET)):
        tokens = _tokens(variant)
        for word in (*tokens, *_spread_letters(tokens)):
            if _WORD.fullmatch(word):
                return True
    return False
