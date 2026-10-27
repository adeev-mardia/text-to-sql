"""Builds a schema-grounded lexicon: phrases that refer to each table and
column, used by the semantic parser to map natural-language mentions onto
real schema elements.

Phrases come from two sources:

1. Automatically derived from the schema itself — splitting snake_case
   names into words and generating a naive singular/plural pair
   ("order_items" -> "order item" / "order items"). This alone makes the
   parser work against ANY schema with no configuration.
2. An optional domain lexicon (a small JSON/dict of extra synonyms a
   deployer can supply for their own database, e.g. "tier" for
   "loyalty_tier", "client" for "customer") that improves recall without
   being required.

Nothing here is a lookup table of whole questions: entries are phrases for
single tables/columns, combined compositionally by the parser.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from text_to_sql.schema import Schema


def _split_identifier(name: str) -> list[str]:
    return [p for p in re.split(r"[_\s]+", name.strip()) if p]


def _naive_singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith("ses") and len(word) > 3:
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 1:
        return word[:-1]
    return word


def _naive_plural(word: str) -> str:
    if word.endswith("y") and word[-2:-1] not in "aeiou":
        return word[:-1] + "ies"
    if word.endswith(("s", "x", "ch", "sh")):
        return word + "es"
    return word + "s"


@dataclass
class Lexicon:
    """Maps lowercase phrases -> ("table", table_name) or
    ("column", table_name, column_name).
    """

    table_phrases: dict[str, str] = field(default_factory=dict)
    column_phrases: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    def all_table_phrases(self) -> list[str]:
        return list(self.table_phrases.keys())

    def all_column_phrases(self) -> list[str]:
        return list(self.column_phrases.keys())


def build_lexicon(schema: Schema, overrides: dict | None = None) -> Lexicon:
    lex = Lexicon()
    overrides = overrides or {}
    extra_tables: dict[str, list[str]] = overrides.get("tables", {})
    extra_columns: dict[str, list[str]] = overrides.get("columns", {})

    for table in schema.tables.values():
        words = _split_identifier(table.name)
        base = " ".join(words)
        singular_words = [_naive_singular(w) for w in words]
        singular = " ".join(singular_words)
        plural_words = singular_words[:-1] + [_naive_plural(singular_words[-1])]
        plural = " ".join(plural_words)
        for phrase in {base, singular, plural, table.name.replace("_", " ")}:
            lex.table_phrases[phrase] = table.name
        for phrase in extra_tables.get(table.name, []):
            lex.table_phrases[phrase.lower()] = table.name

        table_singular = singular  # e.g. "customer", "order item"

        for col_name in table.column_names:
            cwords = _split_identifier(col_name)
            cbase = " ".join(cwords)
            csingular_words = [_naive_singular(w) for w in cwords]
            csingular = " ".join(csingular_words)
            cplural_words = csingular_words[:-1] + [_naive_plural(csingular_words[-1])]
            cplural = " ".join(cplural_words)
            phrases = {cbase, csingular, cplural, col_name.replace("_", " ")}
            # also allow the bare final word (e.g. "date" for order_date)
            if len(cwords) > 1:
                phrases.add(cwords[-1])
                phrases.add(_naive_singular(cwords[-1]))
            for phrase in phrases:
                lex.column_phrases.setdefault(phrase, []).append((table.name, col_name))

            # Compound phrases ("customer name", "product price") are
            # unambiguous even when the bare column name ("name", "price")
            # is shared by several tables, so they get their own entry
            # pointing at exactly one (table, column).
            for base_phrase in {cbase, csingular, cplural}:
                compound = f"{table_singular} {base_phrase}".strip()
                lex.column_phrases.setdefault(compound, []).insert(0, (table.name, col_name))

        for col_name, phrases in extra_columns.items():
            if col_name not in table.columns:
                continue
            for phrase in phrases:
                lex.column_phrases.setdefault(phrase.lower(), []).append((table.name, col_name))

    return lex


def load_overrides(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
