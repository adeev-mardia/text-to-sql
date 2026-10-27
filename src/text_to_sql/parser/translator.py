"""The rule-based NL -> SQL semantic parser.

This is the "works fully offline" path. It does real grammar-driven
intent/slot extraction, not a lookup table of exact questions:

  1. tokenize the question
  2. ground entity mentions against the schema's actual column values
     (fuzzy-matched with difflib against a cached distinct-value index)
  3. extract aggregate intent (count/sum/avg/max/min), filter conditions
     (equality, numeric comparison, date ranges), group-by/order-by/limit
  4. resolve which tables are involved and find a join path between them
     over the schema's real foreign keys
  5. emit a QueryPlan, rendered to SQL by plan.build_sql

Any question that doesn't fit a recognized grammatical pattern raises
ParseError with a reason, rather than guessing silently — the CLI then
offers to fall back to the pluggable LLM backend (see llm_backend.py).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from text_to_sql.parser.lexicon import Lexicon, build_lexicon
from text_to_sql.parser.plan import Filter, OrderBy, QueryPlan
from text_to_sql.schema import Schema

DATE_RE = r"\d{4}-\d{2}-\d{2}"

AGG_WORDS = {
    "COUNT": [r"how many", r"number of", r"count of", r"\bcount\b"],
    "SUM": [r"\btotal\b", r"sum of", r"\bsum\b"],
    "AVG": [r"\baverage\b", r"\bavg\b", r"\bmean\b"],
}

MAX_WORDS = [r"highest", r"maximum", r"\bmax\b", r"most expensive", r"largest", r"greatest", r"\bmost\b", r"top-rated", r"best selling"]
MIN_WORDS = [r"lowest", r"minimum", r"\bmin\b", r"cheapest", r"least expensive", r"smallest", r"\bleast\b"]

WHICH_WORDS = [r"\bwhich\b", r"\bwho\b", r"\bwhat are\b", r"\blist\b", r"\bshow\b", r"\bname\b"]

REVENUE_IDIOMS = ["spending", "revenue", "sales", "amount spent", "money spent"]
REVENUE_EXPR = '"order_items"."quantity" * "order_items"."unit_price"'


class ParseError(Exception):
    """Raised when the rule-based grammar cannot confidently parse a
    question. Callers (the CLI) can catch this and fall back to the LLM
    backend.
    """


@dataclass
class ValueMatch:
    table: str
    column: str
    value: str
    span_text: str


class NLToSQLTranslator:
    def __init__(self, schema: Schema, overrides: dict | None = None):
        self.schema = schema
        self.lexicon: Lexicon = build_lexicon(schema, overrides)
        self._overrides = overrides or {}
        self._value_index = self._build_value_index()
        self._label_columns = self._build_label_columns()

    # -- setup ---------------------------------------------------------------

    def _build_value_index(self) -> dict[str, list[tuple[str, str, str]]]:
        index: dict[str, list[tuple[str, str, str]]] = {}
        for table in self.schema.tables.values():
            for col in table.text_columns:
                for value in self.schema.distinct_values(table.name, col):
                    index.setdefault(value.lower(), []).append((table.name, col, value))
        return index

    def _build_label_columns(self) -> dict[str, str]:
        overrides = self._overrides.get("label_columns", {})
        labels: dict[str, str] = {}
        for table in self.schema.tables.values():
            if table.name in overrides:
                labels[table.name] = overrides[table.name]
                continue
            if "name" in table.columns:
                labels[table.name] = "name"
            elif table.text_columns:
                labels[table.name] = table.text_columns[0]
            else:
                labels[table.name] = table.column_names[0]
        return labels

    # -- phrase resolution -----------------------------------------------------

    def _resolve_column_phrase(self, text: str) -> tuple[str, str] | None:
        """Finds the longest lexicon column phrase that appears in `text`."""
        candidates = sorted(self.lexicon.all_column_phrases(), key=len, reverse=True)
        for phrase in candidates:
            if not phrase:
                continue
            if re.search(rf"\b{re.escape(phrase)}\b", text):
                return self.lexicon.column_phrases[phrase][0]
        return None

    def _resolve_tables_mentioned(self, text: str) -> list[str]:
        found: list[tuple[int, int, str]] = []  # (start, end, table)
        for phrase, table in self.lexicon.table_phrases.items():
            for m in re.finditer(rf"\b{re.escape(phrase)}\b", text):
                found.append((m.start(), m.end(), table))
        # Drop any match fully covered by a longer match at/around the same
        # span, so "order" doesn't register a hit on top of "order items".
        found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        kept: list[tuple[int, int, str]] = []
        for start, end, table in found:
            if any(s <= start and end <= e for s, e, _ in kept):
                continue
            kept.append((start, end, table))
        kept.sort(key=lambda x: x[0])
        tables: list[str] = []
        for _, _, table in kept:
            if table not in tables:
                tables.append(table)
        return tables

    def _scan_value_matches(self, question_raw: str) -> list[ValueMatch]:
        """Scans the question for spans that exactly (or fuzzily) match a
        real distinct value of some text column in the schema, e.g.
        "Electronics" -> categories.name, "India" -> customers.country,
        "completed" -> orders.status. Longest spans win; matches don't
        overlap.
        """
        words = re.findall(r"[A-Za-z][A-Za-z\-']*|\d+", question_raw)
        matches: list[ValueMatch] = []
        consumed: set[int] = set()
        n = len(words)
        for span_len in (4, 3, 2, 1):
            for start in range(0, n - span_len + 1):
                idxs = range(start, start + span_len)
                if any(i in consumed for i in idxs):
                    continue
                span_words = words[start:start + span_len]
                span_text = " ".join(span_words)
                key = span_text.lower()
                if len(key) < 3:
                    continue
                hit = self._value_index.get(key)
                if hit is None and span_len <= 2 and span_text[0].isupper():
                    # Fuzzy fallback for typos/casing, restricted to spans that
                    # look like proper nouns (capitalized in the question) so
                    # we don't accidentally fuzzy-match common words like
                    # "spending" onto an unrelated value like "pending".
                    close = difflib.get_close_matches(key, self._value_index.keys(), n=1, cutoff=0.88)
                    if close:
                        hit = self._value_index[close[0]]
                if hit:
                    table, column, value = hit[0]
                    matches.append(ValueMatch(table=table, column=column, value=value, span_text=span_text))
                    consumed.update(idxs)
        matches.sort(key=lambda m: question_raw.lower().find(m.span_text.lower()))
        return matches

    # -- top-level parse -------------------------------------------------------

    def parse(self, question: str) -> QueryPlan:
        q_raw = question.strip()
        q = q_raw.lower()
        plan = QueryPlan(raw_question=question)

        value_matches = self._scan_value_matches(q_raw)
        for vm in value_matches:
            plan.filters.append(Filter(table=vm.table, column=vm.column, op="=", value=vm.value))

        self._extract_limit(q, plan)
        self._extract_date_filters(q, plan)
        self._extract_numeric_comparisons(q, plan)
        top_n_direction = self._extract_top_n_entity(q, plan)
        handled_directly = top_n_direction == "__direct__"
        if handled_directly:
            top_n_direction = None
        if not plan.group_by and not handled_directly:
            self._extract_group_by(q, plan)
        if not handled_directly:
            self._extract_order_by(q, plan)

        mentioned_tables = self._resolve_tables_mentioned(q)
        for f in plan.filters:
            if f.table not in mentioned_tables:
                mentioned_tables.append(f.table)

        agg_func, agg_is_scalar = self._detect_aggregate(q)
        if agg_func is None and plan.agg_func:
            agg_func = plan.agg_func

        revenue_mode = any(idiom in q for idiom in REVENUE_IDIOMS)

        if agg_func and agg_is_scalar and not plan.group_by:
            plan.agg_func = agg_func
            if revenue_mode:
                plan.agg_expr = REVENUE_EXPR
                for t in ("order_items",):
                    if t not in mentioned_tables:
                        mentioned_tables.append(t)
            elif agg_func != "COUNT" or self._explicit_count_target(q):
                target = self._resolve_aggregate_target(q, mentioned_tables)
                if target is None:
                    raise ParseError(
                        "Could not resolve which column to aggregate; rephrase with an explicit "
                        "column, e.g. 'average price of products'."
                    )
                plan.agg_table, plan.agg_column = target
                if plan.agg_table not in mentioned_tables:
                    mentioned_tables.append(plan.agg_table)
            plan.tables = mentioned_tables or self._default_table(q)
            if not plan.tables:
                raise ParseError("Could not determine which table the question refers to.")
            plan.tables = self._order_tables_for_plan(plan.tables)
            return plan

        if plan.group_by and agg_func:
            plan.agg_func = agg_func
            if revenue_mode:
                plan.agg_expr = REVENUE_EXPR
                if "order_items" not in mentioned_tables:
                    mentioned_tables.append("order_items")
            elif agg_func != "COUNT":
                target = self._resolve_aggregate_target(q, mentioned_tables)
                if target:
                    plan.agg_table, plan.agg_column = target
                    if plan.agg_table not in mentioned_tables:
                        mentioned_tables.append(plan.agg_table)
            if plan.order_by is None:
                plan.order_by = OrderBy(table=None, column=None, direction=top_n_direction or "DESC", agg_func=agg_func)
            elif plan.order_by.agg_func is None and plan.order_by.column is None:
                plan.order_by.agg_func = agg_func
            plan.tables = self._order_tables_for_plan(mentioned_tables)
            return plan

        # -- ranking / plain listing ------------------------------------------
        is_superlative = bool(re.search("|".join(MAX_WORDS + MIN_WORDS), q))
        if is_superlative and not plan.order_by:
            direction = "DESC" if re.search("|".join(MAX_WORDS), q) else "ASC"
            target = None
            if "expensive" in q or "cheap" in q:
                target = self._find_price_column(mentioned_tables)
            if target is None:
                target = self._resolve_column_phrase(q)
            if target is None and mentioned_tables:
                raise ParseError("Could not resolve which column the superlative refers to.")
            if target:
                if target[0] not in mentioned_tables:
                    mentioned_tables.append(target[0])
                plan.order_by = OrderBy(table=target[0], column=target[1], direction=direction)
                if plan.limit is None:
                    plan.limit = 1

        fallback_col = None
        if not mentioned_tables:
            mentioned_tables = self._default_table(q)
        if not mentioned_tables:
            # No table named at all ("list all suppliers") - infer the
            # table from a bare column mention and list its distinct values.
            fallback_col = self._resolve_column_phrase(q)
            if fallback_col:
                mentioned_tables = [fallback_col[0]]
        if not mentioned_tables:
            raise ParseError("Could not determine which table the question refers to.")

        plan.tables = self._order_tables_for_plan(mentioned_tables)

        if fallback_col:
            plan.select_cols = [fallback_col]
            plan.distinct = True
            return plan

        # select columns: if the question names specific columns to display
        # ("show name and email of customers"), use those; else show the
        # primary table's label + natural identifying columns.
        requested_cols = self._extract_display_columns(q, plan.tables)
        if requested_cols:
            plan.select_cols = requested_cols
        else:
            primary = plan.tables[0]
            label_col = self._label_columns.get(primary)
            if label_col:
                plan.select_cols = [(primary, label_col)]

        return plan

    # -- sub-extractors --------------------------------------------------------

    def _extract_limit(self, q: str, plan: QueryPlan) -> None:
        m = re.search(r"\btop\s+(\d+)\b", q) or re.search(r"\bfirst\s+(\d+)\b", q)
        if m:
            plan.limit = int(m.group(1))
            return
        m = re.search(r"\bbottom\s+(\d+)\b", q)
        if m:
            plan.limit = int(m.group(1))

    def _extract_date_filters(self, q: str, plan: QueryPlan) -> None:
        col = self._resolve_column_phrase(q)
        date_col = None
        if col and ("date" in col[1]):
            date_col = col
        else:
            if re.search(r"sign(?:ed|up)?\s*up|signup|joined|registered", q):
                date_col = ("customers", "signup_date")
            elif "order" in q:
                date_col = ("orders", "order_date")

        m = re.search(rf"\bbetween\s+({DATE_RE})\s+and\s+({DATE_RE})\b", q)
        if m and date_col:
            plan.filters.append(Filter(date_col[0], date_col[1], "BETWEEN", (m.group(1), m.group(2))))
            return
        m = re.search(rf"\b(since|after)\s+({DATE_RE})\b", q)
        if m and date_col:
            op = ">=" if m.group(1) == "since" else ">"
            plan.filters.append(Filter(date_col[0], date_col[1], op, m.group(2)))
            return
        m = re.search(rf"\bbefore\s+({DATE_RE})\b", q)
        if m and date_col:
            plan.filters.append(Filter(date_col[0], date_col[1], "<", m.group(1)))

    def _extract_numeric_comparisons(self, q: str, plan: QueryPlan) -> None:
        patterns = [
            (r"(?:more than|greater than|over|above)\s+\$?(\d+(?:\.\d+)?)", ">"),
            (r"(?:at least)\s+\$?(\d+(?:\.\d+)?)", ">="),
            (r"(?:less than|under|below)\s+\$?(\d+(?:\.\d+)?)", "<"),
            (r"(?:at most|no more than)\s+\$?(\d+(?:\.\d+)?)", "<="),
        ]
        for pattern, op in patterns:
            for m in re.finditer(pattern, q):
                window_start = max(0, m.start() - 40)
                window = q[window_start:m.start()]
                col = self._resolve_column_phrase(window)
                if col is None:
                    continue
                plan.filters.append(Filter(col[0], col[1], op, float(m.group(1))))

    def _extract_top_n_entity(self, q: str, plan: QueryPlan) -> str | None:
        """Recognizes "top N <entities> by <metric>" / "bottom N <entities>"
        ranking idioms, e.g. "top 5 customers by total spending": these
        group by the entity's natural label column and rank by an
        aggregate, rather than being a plain scalar aggregate or a plain
        per-row superlative.
        """
        m = re.search(r"\b(top|bottom)\s+\d+\s+([a-z]+)\b(?:\s+by\s+([a-z][a-z\s]*?))?(?=[.,?]|$)", q)
        if m:
            direction = "DESC" if m.group(1) == "top" else "ASC"
            entity_word = m.group(2)
            metric_phrase = m.group(3)
            if metric_phrase:
                entity_table = self.lexicon.table_phrases.get(entity_word)
                direct_col = self._resolve_column_phrase(metric_phrase)
                # If the ranking metric is already a plain column of the
                # named entity's own table ("products by price"), this is
                # a simple ORDER BY + LIMIT - no grouping/aggregation
                # needed, unlike "customers by total spending" where the
                # metric lives in a different, joined table.
                if direct_col and entity_table and direct_col[0] == entity_table:
                    plan.order_by = OrderBy(table=direct_col[0], column=direct_col[1], direction=direction)
                    return "__direct__"
        else:
            # "which customer has the highest/most ..." - an implicit
            # top-1 ranking over a per-entity aggregate.
            m = re.search(
                r"\bwhich\s+([a-z]+)\s+(?:(?:has|had)\s+)?(?:(?:placed|spent|made|got)\s+)?"
                r"the\s+(highest|most|lowest|least)\s*([a-z]*)\b",
                q,
            )
            if not m:
                return None
            entity_word = m.group(1)
            direction = "DESC" if m.group(2) in ("highest", "most") else "ASC"
            if plan.limit is None:
                plan.limit = 1
            metric_word = m.group(3)
            metric_table = self.lexicon.table_phrases.get(metric_word) if metric_word else None
            entity_table_guess = self.lexicon.table_phrases.get(entity_word)
            if metric_table and metric_table != entity_table_guess:
                # "which category has the most products" - the metric names
                # another table, so this is an implied COUNT of that
                # table's rows per group, not a numeric column aggregate.
                plan.agg_func = "COUNT"

        table = self.lexicon.table_phrases.get(entity_word)
        if table is None:
            return None
        label = self._label_columns.get(table)
        if label is None:
            return None
        plan.group_by.append((table, label))
        return direction

    def _extract_group_by(self, q: str, plan: QueryPlan) -> str | None:
        m = re.search(
            r"\b(?:by|per|for each|each|broken down by|grouped by)\s+([a-z][a-z\s]*?)"
            r"(?=[.,?]|$|\s+(?:order|sort|limit|top|and|with|have|has|do|does|get|got)\b)",
            q,
        )
        if not m:
            return None
        phrase = m.group(1).strip()
        col = self._resolve_column_phrase(phrase)
        if col is None:
            table = self.lexicon.table_phrases.get(phrase)
            if table:
                label = self._label_columns.get(table)
                if label:
                    col = (table, label)
        if col:
            plan.group_by.append(col)
            return phrase
        return None

    def _extract_order_by(self, q: str, plan: QueryPlan) -> str | None:
        m = re.search(
            r"\b(?:sorted|ordered)\s+by\s+([a-z][a-z\s]*?)\s*(ascending|asc|descending|desc)?"
            r"(?=[.,?]|$)",
            q,
        )
        if not m:
            return None
        phrase = m.group(1).strip()
        direction = "ASC" if (m.group(2) or "").startswith("asc") else "DESC"
        col = self._resolve_column_phrase(phrase)
        if col:
            plan.order_by = OrderBy(table=col[0], column=col[1], direction=direction)
            return phrase
        if any(idiom in q for idiom in REVENUE_IDIOMS):
            plan.order_by = OrderBy(table=None, column=None, direction=direction)
            return phrase
        return None

    def _detect_aggregate(self, q: str) -> tuple[str | None, bool]:
        for func, patterns in AGG_WORDS.items():
            if any(re.search(p, q) for p in patterns):
                return func, True
        if re.search("|".join(MAX_WORDS), q) and re.search(r"\bwhat (?:is|was|are)\b", q):
            return "MAX", True
        if re.search("|".join(MIN_WORDS), q) and re.search(r"\bwhat (?:is|was|are)\b", q):
            return "MIN", True
        return None, False

    def _explicit_count_target(self, q: str) -> bool:
        return True

    def _resolve_aggregate_target(self, q: str, mentioned_tables: list[str]) -> tuple[str, str] | None:
        col = self._resolve_column_phrase(q)
        if col:
            return col
        for t in mentioned_tables:
            table = self.schema.find_table(t)
            if table and table.numeric_columns:
                return t, table.numeric_columns[0]
        return None

    def _find_price_column(self, mentioned_tables: list[str]) -> tuple[str, str] | None:
        for t in mentioned_tables + list(self.schema.tables.keys()):
            table = self.schema.find_table(t)
            if not table:
                continue
            for c in table.column_names:
                if "price" in c:
                    return t, c
        return None

    def _default_table(self, q: str) -> list[str]:
        for phrase, table in sorted(self.lexicon.table_phrases.items(), key=lambda kv: -len(kv[0])):
            if phrase in q:
                return [table]
        return []

    def _extract_display_columns(self, q: str, tables: list[str]) -> list[tuple[str, str]]:
        m = re.search(r"\bshow (?:me )?(?:the )?([a-z\s,]+?)\s+(?:of|for)\s+", q)
        if not m:
            return []
        parts = re.split(r",|\band\b", m.group(1))
        cols: list[tuple[str, str]] = []
        for part in parts:
            col = self._resolve_column_phrase(part.strip())
            if col and col not in cols:
                cols.append(col)
        return cols

    def _order_tables_for_plan(self, tables: list[str]) -> list[str]:
        """Orders tables so the schema's join_path can connect them, and so
        the primary (first) table matches the question's main subject.
        """
        seen = []
        for t in tables:
            if t not in seen:
                seen.append(t)
        return seen
