from text_to_sql.parser.plan import build_sql
from text_to_sql.parser.translator import NLToSQLTranslator, ParseError


def _translator(schema):
    return NLToSQLTranslator(schema)


def test_aggregate_count_detection(schema):
    t = _translator(schema)
    plan = t.parse("How many customers are there?")
    assert plan.agg_func == "COUNT"
    assert plan.tables == ["customers"]


def test_aggregate_sum_detection_with_revenue_idiom(schema):
    t = _translator(schema)
    plan = t.parse("What is the total revenue?")
    assert plan.agg_func == "SUM"
    assert plan.agg_expr is not None
    assert "order_items" in plan.tables


def test_aggregate_average_detection(schema):
    t = _translator(schema)
    plan = t.parse("What is the average price of products?")
    assert plan.agg_func == "AVG"
    assert plan.agg_table == "products"
    assert plan.agg_column == "price"


def test_equality_filter_grounded_in_real_column_values(schema):
    t = _translator(schema)
    plan = t.parse("Customers in the USA")
    assert any(f.table == "customers" and f.column == "country" and f.value == "USA" for f in plan.filters)


def test_numeric_comparison_filter(schema):
    t = _translator(schema)
    plan = t.parse("Products with price over 100")
    filters = [f for f in plan.filters if f.column == "price"]
    assert len(filters) == 1
    assert filters[0].op == ">"
    assert filters[0].value == 100.0


def test_date_range_filter(schema):
    t = _translator(schema)
    plan = t.parse("Orders placed between 2024-01-01 and 2024-03-01")
    filters = [f for f in plan.filters if f.column == "order_date"]
    assert len(filters) == 1
    assert filters[0].op == "BETWEEN"
    assert filters[0].value == ("2024-01-01", "2024-03-01")


def test_group_by_extraction(schema):
    t = _translator(schema)
    plan = t.parse("Count orders by status")
    assert plan.group_by == [("orders", "status")]
    assert plan.agg_func == "COUNT"


def test_join_resolution_across_fk_path(schema):
    t = _translator(schema)
    plan = t.parse("Show products in the Electronics category")
    sql = build_sql(plan, schema)
    assert '"products"' in sql and '"categories"' in sql
    assert "JOIN" in sql


def test_ranking_superlative_without_grouping(schema):
    t = _translator(schema)
    plan = t.parse("Which product is the most expensive?")
    assert plan.order_by is not None
    assert plan.order_by.direction == "DESC"
    assert plan.limit == 1


def test_top_n_with_aggregate_grouping(schema):
    t = _translator(schema)
    plan = t.parse("Top 5 customers by total spending")
    assert plan.group_by == [("customers", "name")]
    assert plan.agg_func == "SUM"
    assert plan.limit == 5
    assert plan.order_by.direction == "DESC"


def test_unparseable_question_raises_parse_error(schema):
    t = _translator(schema)
    import pytest

    with pytest.raises(ParseError):
        t.parse("asdf qwerty zxcv nonsense")


def test_fuzzy_entity_matching_against_distinct_values(schema):
    # "Electronic" (missing the trailing s) should still fuzzy-match the
    # real category name "Electronics" via the schema's distinct-value index.
    t = _translator(schema)
    plan = t.parse("Show products in the Electronic category")
    assert any(f.column == "name" and f.value == "Electronics" for f in plan.filters)
