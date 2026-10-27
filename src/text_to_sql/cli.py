"""Command-line interface: `text-to-sql`.

Usage:
    text-to-sql path/to.db                      interactive REPL
    text-to-sql path/to.db -q "how many ..."     single question, batch mode
    text-to-sql path/to.db --eval eval/eval_set.json   run the evaluation set
"""

from __future__ import annotations

import argparse
import json
import sys

from text_to_sql.agent import TextToSQLAgent
from text_to_sql.llm_backend import LLMBackend


def _print_table(columns: list[str], rows: list[tuple]) -> None:
    if not columns:
        print("(no result columns)")
        return
    widths = [max(len(str(c)), *(len(str(r[i])) for r in rows)) if rows else len(str(c)) for i, c in enumerate(columns)]
    header = " | ".join(str(c).ljust(w) for c, w in zip(columns, widths))
    print(header)
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(" | ".join(str(v).ljust(w) for v, w in zip(row, widths)))


def _build_llm_backend(args: argparse.Namespace) -> LLMBackend | None:
    if not args.llm_api_key and not args.llm_base_url:
        return None
    return LLMBackend(
        api_key=args.llm_api_key,
        base_url=args.llm_base_url or "https://api.openai.com/v1",
        model=args.llm_model,
    )


def _run_eval(agent: TextToSQLAgent, eval_path: str) -> int:
    with open(eval_path, "r", encoding="utf-8") as f:
        cases = json.load(f)

    correct = 0
    for case in cases:
        question = case["question"]
        gold_sql = case["gold_sql"]
        try:
            result = agent.ask(question)
            got_rows = set(tuple(r) for r in result.rows)
        except Exception as exc:  # noqa: BLE001 - we want to count failures, not crash the eval
            print(f"[FAIL]  {question}\n        error: {exc}")
            continue

        cur = agent.conn.execute(gold_sql)
        gold_rows = set(tuple(r) for r in cur.fetchall())

        ok = got_rows == gold_rows
        correct += int(ok)
        status = "PASS" if ok else "WRONG"
        print(f"[{status}] {question}")
        if not ok:
            print(f"        got:  {result.sql}")
            print(f"        gold: {gold_sql}")

    total = len(cases)
    accuracy = correct / total if total else 0.0
    print(f"\nExecution accuracy: {correct}/{total} = {accuracy:.1%}")
    return 0 if accuracy == 1.0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ask natural-language questions against a SQLite database.")
    parser.add_argument("db_path", help="Path to a SQLite database file")
    parser.add_argument("-q", "--question", help="Ask a single question and exit (batch mode)")
    parser.add_argument("--eval", dest="eval_path", help="Run an evaluation set (JSON file) and report accuracy")
    parser.add_argument("--lexicon", dest="lexicon_path", help="Path to a JSON lexicon-overrides file")
    parser.add_argument("--sql-only", action="store_true", help="Print only the generated SQL, don't execute it")
    parser.add_argument("--llm-api-key", help="API key for the pluggable LLM backend (enables LLM fallback)")
    parser.add_argument("--llm-base-url", help="Base URL for an OpenAI-compatible LLM endpoint")
    parser.add_argument("--llm-model", default="gpt-4o-mini", help="Model name for the LLM backend")
    args = parser.parse_args(argv)

    llm_backend = _build_llm_backend(args)
    overrides = None
    if args.lexicon_path:
        from text_to_sql.parser.lexicon import load_overrides

        overrides = load_overrides(args.lexicon_path)
    agent = TextToSQLAgent(args.db_path, overrides=overrides, llm_backend=llm_backend)

    try:
        if args.eval_path:
            return _run_eval(agent, args.eval_path)

        if args.question:
            return _ask_once(agent, args.question, args.sql_only)

        return _repl(agent, args.sql_only)
    finally:
        agent.close()


def _ask_once(agent: TextToSQLAgent, question: str, sql_only: bool) -> int:
    try:
        if sql_only:
            plan = agent.translator.parse(question)
            from text_to_sql.parser.plan import build_sql

            print(build_sql(plan, agent.schema))
            return 0
        result = agent.ask(question)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"SQL: {result.sql}")
    if result.used_llm_fallback:
        print("(answered via LLM fallback)")
    if result.correction_attempts > 1:
        print(f"(self-corrected after {result.correction_attempts - 1} failed attempt(s))")
    _print_table(result.columns, result.rows)
    return 0


def _repl(agent: TextToSQLAgent, sql_only: bool) -> int:
    print(f"Connected to {agent.db_path}. Tables: {', '.join(agent.schema.table_names())}")
    print("Ask a question in plain English, or type 'exit' to quit.")
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not question:
            continue
        if question.lower() in {"exit", "quit"}:
            return 0
        _ask_once(agent, question, sql_only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
