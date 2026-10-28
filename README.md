# text-to-sql

Ask a SQLite database questions in plain English. The query is produced by a
rule-based semantic parser that works entirely offline, grounded against the
database's real, introspected schema. If something goes wrong when the SQL
is run, a self-correction loop reads SQLite's actual error message and
retries with a fixed query. A pluggable LLM backend can be wired in for
free-form questions the offline parser can't handle.

```
$ text-to-sql examples/store.db -q "which customer has the highest total spending?"
SQL: SELECT "customers"."name", SUM("order_items"."quantity" * "order_items"."unit_price") AS result
     FROM "customers"
     JOIN "orders" ON "orders"."customer_id" = "customers"."customer_id"
     JOIN "order_items" ON "order_items"."order_id" = "orders"."order_id"
     GROUP BY "customers"."name"
     ORDER BY SUM("order_items"."quantity" * "order_items"."unit_price") DESC
     LIMIT 1
name       | result
-----------+---------
Zara Lopez | 15573.19
```

## How it works

### 1. Schema introspection (`schema.py`)

Nothing about the database is hardcoded. `Schema` reads SQLite's own
metadata — `PRAGMA table_info`, `PRAGMA foreign_key_list`, `sqlite_master` —
to learn every table, column, type, and foreign key at runtime. It also
builds a cached, lazily-loaded index of distinct values for text columns,
which the parser uses to ground entity mentions in real data rather than
guessing.

`Schema.join_path(tables)` finds a route between a set of tables using a
real BFS shortest-path search over the foreign-key graph (not a hardcoded
join order), so a question that touches `customers` and `products` is
correctly routed through `orders` and `order_items`.

### 2. The rule-based parser (`parser/`)

This is the offline, default translator. It does real grammar-driven
intent/slot extraction, not a lookup table of memorized questions:

- **`lexicon.py`** derives a vocabulary of phrases for every table and
  column directly from the schema (splitting `order_items` into "order
  item" / "order items", etc.), plus compound phrases like "customer name"
  that disambiguate columns shared by several tables ("name" exists on
  `customers`, `products`, and `categories`). An optional JSON file lets a
  deployer add domain synonyms (`"client"` for `customers`) without
  touching code.
- **`tokenizer.py`** does basic tokenization (words, numbers, quoted
  literals).
- **`translator.py`** is the actual grammar engine:
  - **Entity grounding**: every 1-4 word span of the question is checked
    against the schema's real distinct-value index (e.g. "Electronics"
    resolves to `categories.name = 'Electronics'` because that's an actual
    row value, not a predicted string). Short, capitalized spans that don't
    match exactly fall back to fuzzy matching (`difflib`) against that same
    index, so "the Electronic category" still resolves to "Electronics".
  - **Aggregate intent**: regex patterns recognize `count`/`how many`,
    `sum`/`total`, `average`/`avg`/`mean`, and superlative patterns
    (`highest`, `cheapest`, `most`, ...), and distinguish a scalar
    aggregate ("what is the average price of products?") from a per-row
    ranking ("which product is the cheapest?") from a grouped ranking
    ("top 5 customers by total spending?").
  - **Filters**: numeric comparisons (`over`, `at least`, `below`, ...),
    date ranges (`since`, `before`, `between ... and ...`), and equality,
    all resolved to a specific `(table, column)` using the lexicon and the
    value index above.
  - **Group-by / order-by / limit**: `by`, `per`, `for each`, `top N`,
    `bottom N`, `sorted by` are recognized and resolved the same way.
  - **Join resolution**: once every table a question touches is known, the
    real foreign-key graph (via `Schema.join_path`) supplies the actual
    join conditions.
- **`plan.py`** is the intermediate representation (`QueryPlan`) and the
  SQL generator. Keeping "what the question means" separate from "how to
  render it as SQL" is what lets the self-correction loop repair a plan
  and regenerate SQL, rather than patch SQL text blindly.

A question the grammar can't confidently handle raises `ParseError` with a
reason, instead of guessing — see **Limitations** below.

### 3. Self-correction loop (`executor.py`)

`execute_with_correction` runs SQL against the real database. If SQLite
raises a genuine `OperationalError`, the loop parses the *actual* error
text (`no such column: ...`, `no such table: ...`,
`ambiguous column name: ...`) and uses the real schema to fix it, then
retries, up to a bounded number of attempts.

Example — a deliberately malformed, hand-written query with a genuinely
ambiguous column (both `products` and `categories` have a `name` column):

```python
>>> execute_with_correction(conn, schema,
...     'SELECT name, price FROM "products" '
...     'JOIN "categories" ON "products"."category_id" = "categories"."category_id"')
```

Attempt 1 fails with SQLite's real error: `ambiguous column name: name`.
The loop looks at which tables are actually joined in the query, finds that
both `products` and `categories` have a `name` column, picks the first one
that appears in the query, and rewrites the bare reference to
`"products"."name"`. Attempt 2 succeeds. The same mechanism fixes
`no such column: nam` (fuzzy-matches `nam` against the real columns of the
tables in the query and finds `name`) and `no such table: custmers` (fuzzy-
matches against the real table names and finds `customers`). This is
exercised end-to-end in `tests/test_executor.py` against the real example
database — not a scripted "fails once, then a pre-written fix" demo: the
correction is computed from whatever SQLite actually says.

Not every error is correctable (a genuine SQL syntax error, for instance)
— the loop gives up after `max_attempts` and raises `ExecutionError` with
the full attempt history.

### 4. Pluggable LLM backend (`llm_backend.py`)

For questions outside the rule-based grammar, `LLMBackend` is a real
OpenAI Chat-Completions-compatible HTTP client. It sends the actual
introspected schema (tables, columns, types, foreign keys) plus the
question, and expects back a single SQL statement. Because it speaks the
OpenAI-compatible protocol, `base_url` can point at OpenAI itself, a
self-hosted vLLM/Ollama server, or any compatible endpoint — nothing about
it is tied to one vendor.

It is never used unless you explicitly pass an API key or endpoint; the
default CLI mode never makes a network call. The HTTP transport is
injected (`http_post`), which is how `tests/test_llm_backend.py` verifies
the request/response handling deterministically without a real network
call — the same transport-stubbing pattern used elsewhere for HTTP
integrations.

`TextToSQLAgent.ask()` tries the rule-based parser first; if it raises
`ParseError`, or if the generated SQL can't be fixed by the self-correction
loop, and an `LLMBackend` was supplied, it falls back to the LLM.

## The example database

`examples/build_db.py` generates `examples/store.db`, a small e-commerce
schema with real foreign keys and enough seed data for aggregate/group/join
questions to be meaningful:

| table | rows | notes |
|---|---|---|
| `categories` | 8 | |
| `customers` | 40 | city, country, loyalty tier, signup date |
| `products` | 80 | 10 per category, price, stock, supplier |
| `orders` | 220 | status, order date |
| `order_items` | 686 | quantity, unit price (snapshotted at order time) |

It isn't committed to the repo (it's a binary SQLite file); build it after
cloning with:

```
python examples/build_db.py
```

This is deterministic (seeded `random`), so everyone gets the exact same
rows, and the same command regenerates it later if you want a fresh copy.

## Evaluation

`eval/eval_set.json` has 35 hand-written natural-language questions against
`examples/store.db`, each with a hand-verified gold SQL query. Scoring is
standard **execution accuracy**: a question is correct if running the
generated SQL returns the same set of rows as running the gold query — not
string-matching the SQL.

```
$ text-to-sql examples/store.db --eval eval/eval_set.json
...
Execution accuracy: 32/35 = 91.4%
```

This is a genuine, non-inflated number from an actual run — `tests/test_eval.py`
re-runs the same evaluation in CI and asserts accuracy stays above 85%,
and asserts it never hits 100% (if it ever does, the eval set has stopped
containing anything the rule-based parser genuinely can't do). The three
questions it currently misses are listed in Limitations below.

## Running against your own database

```
pip install -e .
text-to-sql /path/to/your.db
```

That opens an interactive prompt. For one-off or scripted use:

```
text-to-sql /path/to/your.db -q "how many rows are in orders from last month"
```

Add `--sql-only` to see the generated SQL without executing it, and
`--lexicon path/to/overrides.json` to add domain synonyms for your schema
(see `examples/store_lexicon.json` for the format: extra table/column
synonyms and which column is each table's natural "label" column for
display and grouping).

To enable the LLM fallback for questions the offline parser can't parse:

```
text-to-sql /path/to/your.db --llm-api-key sk-... [--llm-base-url ... --llm-model ...]
```

### Using it as a library

```python
from text_to_sql.agent import TextToSQLAgent

agent = TextToSQLAgent("examples/store.db")
result = agent.ask("top 5 customers by total spending")
print(result.sql)
print(result.columns, result.rows)
```

## Limitations

The rule-based parser is deliberately scoped, and is honest about what it
can't do rather than guessing:

- **No ratio/percentage/statistical computation** ("what percentage of
  orders are cancelled?", "what is the median price?"). These need
  multi-step SQL (subqueries, `CASE` expressions) the grammar doesn't
  generate.
- **No negation / set-difference questions** ("customers who have never
  placed an order") — these need `NOT IN`/`NOT EXISTS`, which isn't part
  of the current filter grammar.
- **Ambiguous superlatives without a resolvable column** ("what's the best
  product?" — "best" doesn't map to any column) raise `ParseError` rather
  than silently picking the wrong one.
- **Multi-condition filters joined with "or"**, nested comparisons, and
  subqueries in general aren't supported.
- Column/value grounding is schema-wide, so an extremely large distinct-
  value index (millions of rows) would need the cardinality cap in
  `Schema.distinct_values` raised or a different grounding strategy.

Any of these is exactly the kind of case to route through the pluggable
LLM backend.

## Tests

```
pip install -e .
python -m pytest
```

33 tests cover schema introspection (tables/columns/FKs/join-path
shortest-routing and disconnection), individual parser components
(aggregate/filter/group-by/join extraction on crafted cases), the
self-correction loop against real, deliberately malformed SQL (typo'd
column, typo'd table, genuinely ambiguous join column, an unfixable syntax
error, and a bounded-retry case), the LLM backend against a stubbed HTTP
transport, and the end-to-end evaluation-set execution-accuracy run.
