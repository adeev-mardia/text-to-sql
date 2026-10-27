"""Executes generated SQL against the real database and, when SQLite
raises a real execution error, uses that error message plus the actual
schema to correct the query and retry — a genuine execution-based
self-correction loop, bounded to a small number of attempts.

This module works on raw SQL text, not on the parser's QueryPlan, so it
corrects whatever produced the query: the rule-based parser, the LLM
backend, or SQL a user typed by hand.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
from dataclasses import dataclass, field

from text_to_sql.schema import Schema


@dataclass
class Attempt:
    sql: str
    error: str | None


@dataclass
class ExecutionResult:
    sql: str
    columns: list[str]
    rows: list[tuple]
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def was_corrected(self) -> bool:
        return self.attempt_count > 1


class ExecutionError(Exception):
    def __init__(self, message: str, attempts: list[Attempt]):
        super().__init__(message)
        self.attempts = attempts


_NO_SUCH_COLUMN = re.compile(r"no such column:\s*(\S+)")
_NO_SUCH_TABLE = re.compile(r"no such table:\s*(\S+)")
_AMBIGUOUS_COLUMN = re.compile(r"ambiguous column name:\s*(\S+)")

_TABLE_REF_RE = re.compile(r'(?:FROM|JOIN)\s+"([A-Za-z_][A-Za-z0-9_]*)"', re.IGNORECASE)


def _tables_in_sql(sql: str) -> list[str]:
    return _TABLE_REF_RE.findall(sql)


def _correct(sql: str, error_message: str, schema: Schema, tried: set[str]) -> str | None:
    tables_in_query = _tables_in_sql(sql)

    m = _NO_SUCH_TABLE.search(error_message)
    if m:
        bad = m.group(1).strip('"')
        key = f"table:{bad}"
        if key in tried:
            return None
        tried.add(key)
        candidates = schema.table_names()
        match = difflib.get_close_matches(bad, candidates, n=1, cutoff=0.4)
        if not match:
            return None
        fixed = sql.replace(f'"{bad}"', f'"{match[0]}"')
        return fixed if fixed != sql else None

    m = _AMBIGUOUS_COLUMN.search(error_message)
    if m:
        bad = m.group(1).strip('"')
        col_name = bad.split(".")[-1]
        key = f"ambiguous:{col_name}"
        if key in tried:
            return None
        tried.add(key)
        owners = [t for t in tables_in_query if t in schema.tables and col_name in schema.tables[t].columns]
        if not owners:
            owners = schema.columns_named(col_name)
        if not owners:
            return None
        preferred = owners[0]
        # Replace a bare, unqualified reference to the column (not already
        # preceded by a table-qualifying dot) with an explicit qualification.
        pattern = re.compile(rf'(?<!\.)(?<!")\b{re.escape(col_name)}\b(?!")')

        def _qualify(match_obj: re.Match) -> str:
            start = match_obj.start()
            # Skip if immediately preceded by `"table".` already (defensive).
            prefix = sql[max(0, start - 1):start]
            if prefix == ".":
                return match_obj.group(0)
            return f'"{preferred}"."{col_name}"'

        fixed = pattern.sub(_qualify, sql)
        return fixed if fixed != sql else None

    m = _NO_SUCH_COLUMN.search(error_message)
    if m:
        bad = m.group(1).strip('"')
        if "." in bad:
            table_part, col_part = bad.split(".", 1)
        else:
            table_part, col_part = None, bad
        key = f"column:{bad}"
        if key in tried:
            return None
        tried.add(key)

        if table_part:
            table_candidates = difflib.get_close_matches(table_part, schema.table_names(), n=1, cutoff=0.4)
            table_name = table_candidates[0] if table_candidates else table_part
            table_info = schema.find_table(table_name)
            col_pool = table_info.column_names if table_info else []
        else:
            table_name = None
            pool_tables = tables_in_query or schema.table_names()
            col_pool = []
            for t in pool_tables:
                ti = schema.find_table(t)
                if ti:
                    col_pool.extend(ti.column_names)

        match = difflib.get_close_matches(col_part, col_pool, n=1, cutoff=0.4)
        if not match:
            return None
        new_col = match[0]

        if table_part:
            fixed = sql.replace(f'"{table_part}"."{col_part}"', f'"{table_name}"."{new_col}"')
            fixed = fixed.replace(f"{table_part}.{col_part}", f'"{table_name}"."{new_col}"')
        else:
            pattern = re.compile(rf'\b{re.escape(col_part)}\b')
            fixed = pattern.sub(new_col, sql)
        return fixed if fixed != sql else None

    return None


def execute_with_correction(
    conn: sqlite3.Connection, schema: Schema, sql: str, max_attempts: int = 4
) -> ExecutionResult:
    current_sql = sql
    attempts: list[Attempt] = []
    tried: set[str] = set()

    for _ in range(max_attempts):
        try:
            cur = conn.execute(current_sql)
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
            attempts.append(Attempt(sql=current_sql, error=None))
            return ExecutionResult(sql=current_sql, columns=columns, rows=rows, attempts=attempts)
        except sqlite3.OperationalError as exc:
            error_text = str(exc)
            attempts.append(Attempt(sql=current_sql, error=error_text))
            corrected = _correct(current_sql, error_text, schema, tried)
            if corrected is None:
                raise ExecutionError(
                    f"Failed after {len(attempts)} attempt(s); last error: {error_text}", attempts
                ) from exc
            current_sql = corrected

    raise ExecutionError(f"Exceeded {max_attempts} correction attempts", attempts)
