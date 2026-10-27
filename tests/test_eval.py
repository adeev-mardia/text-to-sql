"""End-to-end execution-accuracy test over the hand-verified evaluation set.

This asserts a real, honest accuracy floor rather than 100% - the eval set
deliberately includes a few questions outside what the rule-based grammar
covers (ratio/percentage computation, negation/"never" queries, median), so
this number is never inflated.
"""

import json
import os

EVAL_PATH = os.path.join(os.path.dirname(__file__), "..", "eval", "eval_set.json")
MIN_ACCURACY = 0.85


def test_execution_accuracy_meets_honest_floor(agent):
    with open(EVAL_PATH, "r", encoding="utf-8") as f:
        cases = json.load(f)

    correct = 0
    failures = []
    for case in cases:
        try:
            result = agent.ask(case["question"])
            got_rows = set(tuple(r) for r in result.rows)
        except Exception as exc:  # noqa: BLE001
            failures.append((case["question"], f"error: {exc}"))
            continue

        gold_rows = set(tuple(r) for r in agent.conn.execute(case["gold_sql"]).fetchall())
        if got_rows == gold_rows:
            correct += 1
        else:
            failures.append((case["question"], f"got {got_rows!r} != gold {gold_rows!r}"))

    accuracy = correct / len(cases)
    assert accuracy >= MIN_ACCURACY, (
        f"execution accuracy {accuracy:.1%} below floor {MIN_ACCURACY:.0%}; failures: {failures}"
    )
    # A sane upper bound too: if this ever hits 100%, the eval set has
    # stopped including any genuinely hard question, which would be worth
    # noticing.
    assert correct < len(cases), "eval set should include at least one case the rule-based parser can't handle"
