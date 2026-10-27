from text_to_sql.schema import load_schema


def test_introspects_all_tables(schema):
    assert set(schema.table_names()) == {
        "categories", "customers", "products", "orders", "order_items",
    }


def test_introspects_real_columns_and_types(schema):
    customers = schema.find_table("customers")
    assert "name" in customers.columns
    assert "email" in customers.columns
    assert customers.columns["customer_id"].primary_key
    assert "INT" in customers.columns["customer_id"].type.upper()
    assert "price" in schema.find_table("products").numeric_columns


def test_introspects_foreign_keys(schema):
    products = schema.find_table("products")
    assert any(fk.to_table == "categories" for fk in products.foreign_keys)
    order_items = schema.find_table("order_items")
    targets = {fk.to_table for fk in order_items.foreign_keys}
    assert targets == {"orders", "products"}


def test_distinct_values_are_cached_and_real(schema):
    values = schema.distinct_values("categories", "name")
    assert "Electronics" in values
    assert len(values) == 8


def test_join_path_direct_edge(schema):
    path = schema.join_path(["products", "categories"])
    assert len(path) == 1
    assert {path[0].from_table, path[0].to_table} == {"products", "categories"}


def test_join_path_prefers_shortest_route(schema):
    # orders and order_items are directly linked; the path must not detour
    # through customers even though customers is also one hop from orders.
    path = schema.join_path(["orders", "order_items"])
    assert len(path) == 1
    tables = {path[0].from_table, path[0].to_table}
    assert tables == {"orders", "order_items"}


def test_join_path_multi_hop(schema):
    path = schema.join_path(["customers", "products"])
    touched = {t for fk in path for t in (fk.from_table, fk.to_table)}
    assert {"customers", "orders", "order_items", "products"}.issubset(touched)


def test_join_path_trivial_cases(schema):
    assert schema.join_path([]) == []
    assert schema.join_path(["products"]) == []


def test_join_path_raises_on_disconnected_tables():
    import sqlite3

    import pytest

    from text_to_sql.schema import Schema

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE islands (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE mainland (id INTEGER PRIMARY KEY, name TEXT);
        """
    )
    disconnected_schema = Schema(conn)
    with pytest.raises(ValueError):
        disconnected_schema.join_path(["islands", "mainland"])
