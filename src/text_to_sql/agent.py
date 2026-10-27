"""Ties schema introspection, the rule-based parser, the optional LLM
fallback, and the self-correction executor into one entry point.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from text_to_sql.executor import ExecutionError, ExecutionResult, execute_with_correction
from text_to_sql.llm_backend import LLMBackend
from text_to_sql.parser.plan import build_sql
from text_to_sql.parser.translator import NLToSQLTranslator, ParseError
from text_to_sql.schema import Schema


@dataclass
class AnswerResult:
    question: str
    sql: str
    columns: list[str]
    rows: list[tuple]
    used_llm_fallback: bool
    correction_attempts: int


class TextToSQLAgent:
    def __init__(self, db_path: str, overrides: dict | None = None, llm_backend: LLMBackend | None = None):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.schema: Schema = Schema(self.conn)
        self.translator = NLToSQLTranslator(self.schema, overrides)
        self.llm_backend = llm_backend

    def ask(self, question: str, max_attempts: int = 4) -> AnswerResult:
        used_llm = False
        try:
            plan = self.translator.parse(question)
            sql = build_sql(plan, self.schema)
        except ParseError:
            if self.llm_backend is None:
                raise
            sql = self.llm_backend.translate(question, self.schema)
            used_llm = True

        try:
            result: ExecutionResult = execute_with_correction(self.conn, self.schema, sql, max_attempts)
        except ExecutionError:
            if used_llm or self.llm_backend is None:
                raise
            sql = self.llm_backend.translate(question, self.schema)
            used_llm = True
            result = execute_with_correction(self.conn, self.schema, sql, max_attempts)

        return AnswerResult(
            question=question,
            sql=result.sql,
            columns=result.columns,
            rows=result.rows,
            used_llm_fallback=used_llm,
            correction_attempts=result.attempt_count,
        )

    def close(self) -> None:
        self.conn.close()


def open_agent(db_path: str, lexicon_overrides_path: str | None = None, llm_backend: LLMBackend | None = None) -> TextToSQLAgent:
    overrides = None
    if lexicon_overrides_path:
        from text_to_sql.parser.lexicon import load_overrides

        overrides = load_overrides(lexicon_overrides_path)
    return TextToSQLAgent(db_path, overrides, llm_backend)
