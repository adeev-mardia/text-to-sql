"""A small tokenizer for the rule-based NL-to-SQL parser.

Splits a question into a flat token stream: words (lowercased), numbers
(kept as Decimal-friendly strings), quoted string literals (kept verbatim,
quotes stripped), and punctuation that carries grammatical meaning (commas,
question marks are dropped; hyphens inside words are kept, e.g. "noise-
cancelling").
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TOKEN_RE = re.compile(
    r"""
    (?P<quoted>"[^"]*"|'[^']*')      |
    (?P<number>\d+(?:\.\d+)?)        |
    (?P<word>[A-Za-z][A-Za-z\-]*)    |
    (?P<punct>[^\sA-Za-z0-9])
    """,
    re.VERBOSE,
)

STOPWORDS = {
    "a", "an", "the", "of", "please", "me", "to", "please", "do", "does", "is", "are",
    "was", "were", "for", "give", "show", "list", "find", "get", "tell", "can", "you",
}


@dataclass
class Token:
    text: str      # normalized text (lowercased for words)
    raw: str       # original surface form
    kind: str      # "word" | "number" | "quoted" | "punct"


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    for match in _TOKEN_RE.finditer(text):
        kind = match.lastgroup
        raw = match.group()
        if kind == "quoted":
            tokens.append(Token(text=raw[1:-1], raw=raw[1:-1], kind="quoted"))
        elif kind == "number":
            tokens.append(Token(text=raw, raw=raw, kind="number"))
        elif kind == "word":
            tokens.append(Token(text=raw.lower(), raw=raw, kind="word"))
        elif kind == "punct":
            if raw in "?,.!":
                continue
            tokens.append(Token(text=raw, raw=raw, kind="punct"))
    return tokens


def words(tokens: list[Token]) -> list[str]:
    return [t.text for t in tokens if t.kind == "word"]


def strip_stopwords(tokens: list[str]) -> list[str]:
    return [t for t in tokens if t not in STOPWORDS]
