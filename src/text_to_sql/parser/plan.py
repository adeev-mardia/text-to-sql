"""The intermediate representation the rule-based parser builds, and the
SQL generator that turns it into an actual query string.

Separating "what the question means" (QueryPlan) from "how to render it as
SQL" (build_sql) is what lets the self-correction loop repair a plan (e.g.
re-qualify a column, swap a misresolved table) and regenerate SQL, instead
of patching SQL text directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from text_to_sql.schema import Schema

ComparisonOp = Literal["=", "!=", ">", ">=", "<", "<=", "LIKE", "BETWEEN"]


@dataclass
class Filter:
    table: str
    column: str
    op: ComparisonOp
    value: object  # str | float | int | tuple for BETWEEN


@dataclass
class OrderBy:
    table: str | None   # None when ordering by an aggregate expression
    column: str | None
    direction: Literal["ASC", "DESC"] = "DESC"
    agg_func: str | None = None  # set when ordering by an aggregate, e.g. SUM


@dataclass
class QueryPlan:
    tables: list[str] = field(default_factory=list)
    select_cols: list[tuple[str, str]] = field(default_factory=list)
    agg_func: str | None = None          # COUNT / SUM / AVG / MAX / MIN
    agg_table: str | None = None
    agg_column: str | None = None
    agg_expr: str | None = None          # raw SQL expr overriding agg_table/agg_column (e.g. revenue)
    agg_alias: str = "result"
    distinct: bool = False
    filters: list[Filter] = field(default_factory=list)
    group_by: list[tuple[str, str]] = field(default_factory=list)
    order_by: OrderBy | None = None
    limit: int | None = None
    raw_question: str = ""

    def all_tables(self) -> set[str]:
        tables = set(self.tables)
        for t, _ in self.select_cols:
            tables.add(t)
        for t, _ in self.group_by:
            tables.add(t)
        for f in self.filters:
            tables.add(f.table)
        if self.agg_table:
            tables.add(self.agg_table)
        if self.order_by and self.order_by.table:
            tables.add(self.order_by.table)
        return tables


def _quote_value(value: object) -> str:
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("'", "''")
    return f"'{text}'"


def _filter_sql(f: Filter) -> str:
    col = f'"{f.table}"."{f.column}"'
    if f.op == "BETWEEN":
        lo, hi = f.value
        return f"{col} BETWEEN {_quote_value(lo)} AND {_quote_value(hi)}"
    if f.op == "LIKE":
        return f"{col} LIKE {_quote_value(f.value)}"
    return f"{col} {f.op} {_quote_value(f.value)}"


def build_sql(plan: QueryPlan, schema: Schema) -> str:
    """Renders a QueryPlan into an executable SQL string, resolving joins
    via the schema's real foreign-key graph.
    """
    tables = list(dict.fromkeys(plan.tables)) or list(dict.fromkeys(t for t in plan.all_tables()))
    if not tables:
        raise ValueError("Query plan has no tables")

    primary = tables[0]
    join_edges = schema.join_path(tables) if len(tables) > 1 else []

    from_clause = f'FROM "{primary}" '
    joined = {primary}
    # Order edges so each new join can attach to an already-joined table.
    remaining_edges = list(join_edges)
    join_sql_parts = []
    while remaining_edges:
        progressed = False
        for edge in list(remaining_edges):
            if edge.from_table in joined and edge.to_table not in joined:
                join_sql_parts.append(
                    f'JOIN "{edge.to_table}" ON "{edge.from_table}"."{edge.from_column}" '
                    f'= "{edge.to_table}"."{edge.to_column}" '
                )
                joined.add(edge.to_table)
                remaining_edges.remove(edge)
                progressed = True
            elif edge.to_table in joined and edge.from_table not in joined:
                join_sql_parts.append(
                    f'JOIN "{edge.from_table}" ON "{edge.from_table}"."{edge.from_column}" '
                    f'= "{edge.to_table}"."{edge.to_column}" '
                )
                joined.add(edge.from_table)
                remaining_edges.remove(edge)
                progressed = True
        if not progressed:
            break  # disconnected leftover edges; shouldn't happen given join_path's guarantee

    # -- SELECT -------------------------------------------------------------
    select_parts: list[str] = []
    if plan.group_by:
        for t, c in plan.group_by:
            select_parts.append(f'"{t}"."{c}"')
        if plan.agg_func:
            expr = plan.agg_expr or f'"{plan.agg_table}"."{plan.agg_column}"'
            if plan.agg_func == "COUNT" and plan.agg_expr is None and plan.agg_column is None:
                expr = "*"
            select_parts.append(f'{plan.agg_func}({expr}) AS {plan.agg_alias}')
        elif plan.select_cols:
            for t, c in plan.select_cols:
                if (t, c) not in plan.group_by:
                    select_parts.append(f'"{t}"."{c}"')
    elif plan.agg_func:
        expr = plan.agg_expr or (
            f'"{plan.agg_table}"."{plan.agg_column}"' if plan.agg_column else "*"
        )
        distinct_kw = "DISTINCT " if plan.distinct else ""
        select_parts.append(f'{plan.agg_func}({distinct_kw}{expr}) AS {plan.agg_alias}')
    elif plan.select_cols:
        distinct_kw = "DISTINCT " if plan.distinct else ""
        cols = ", ".join(f'"{t}"."{c}"' for t, c in plan.select_cols)
        select_parts = [f"{distinct_kw}{cols}"]
    else:
        select_parts = ["*"]

    sql = f"SELECT {', '.join(select_parts)} " + from_clause + "".join(join_sql_parts)

    if plan.filters:
        sql += "WHERE " + " AND ".join(_filter_sql(f) for f in plan.filters) + " "

    if plan.group_by:
        sql += "GROUP BY " + ", ".join(f'"{t}"."{c}"' for t, c in plan.group_by) + " "

    if plan.order_by:
        ob = plan.order_by
        if ob.agg_func:
            expr = plan.agg_expr or (f'"{ob.table}"."{ob.column}"' if ob.column else "*")
            if ob.agg_func == "COUNT" and plan.agg_expr is None and ob.column is None:
                expr = "*"
            sql += f"ORDER BY {ob.agg_func}({expr}) {ob.direction} "
        else:
            sql += f'ORDER BY "{ob.table}"."{ob.column}" {ob.direction} '

    if plan.limit is not None:
        sql += f"LIMIT {plan.limit} "

    return sql.strip()
