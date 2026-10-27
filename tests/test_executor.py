import sqlite3

import pytest

from text_to_sql.executor import ExecutionError, execute_with_correction


def test_executes_valid_sql_on_first_attempt(agent):
    result = execute_with_correction(agent.conn, agent.schema, 'SELECT "name" FROM "customers" LIMIT 1')
    assert result.attempt_count == 1
    assert not result.was_corrected


def test_corrects_a_typo_d_unqualified_column_name(agent):
    # A deliberately malformed query: "nam" instead of "name". SQLite
    # genuinely raises "no such column: nam"; the loop must catch that
    # real error and fix it by fuzzy-matching against the real schema.
    bad_sql = "SELECT nam FROM customers LIMIT 3"
    result = execute_with_correction(agent.conn, agent.schema, bad_sql)
    assert result.was_corrected
    assert result.columns == ["name"]
    assert len(result.rows) == 3


def test_corrects_a_typo_d_qualified_column_name(agent):
    bad_sql = 'SELECT "products"."prics" FROM "products" LIMIT 3'
    result = execute_with_correction(agent.conn, agent.schema, bad_sql)
    assert result.was_corrected
    assert result.columns == ["price"]


def test_corrects_a_typo_d_table_name(agent):
    bad_sql = 'SELECT "name" FROM "custmers" LIMIT 2'
    result = execute_with_correction(agent.conn, agent.schema, bad_sql)
    assert result.was_corrected
    assert '"customers"' in result.sql
    assert len(result.rows) == 2


def test_corrects_a_genuinely_ambiguous_join_column(agent):
    # Both products and categories have a "name" column; selecting it bare
    # after a real JOIN is a genuine SQLite "ambiguous column name" error,
    # not a scripted/fake one.
    bad_sql = (
        'SELECT name, price FROM "products" '
        'JOIN "categories" ON "products"."category_id" = "categories"."category_id" LIMIT 5'
    )
    with pytest.raises(sqlite3.OperationalError):
        agent.conn.execute(bad_sql)

    result = execute_with_correction(agent.conn, agent.schema, bad_sql)
    assert result.was_corrected
    assert len(result.rows) == 5


def test_gives_up_after_max_attempts_on_an_unfixable_query(agent):
    with pytest.raises(ExecutionError):
        execute_with_correction(agent.conn, agent.schema, "SELECT * FROM products WHERE (((", max_attempts=2)


def test_correction_loop_is_bounded(agent):
    # Even a column that doesn't fuzzy-match anything real should stop
    # after a bounded number of attempts, not loop forever.
    bad_sql = "SELECT zzz_totally_unknown_zzz FROM products LIMIT 1"
    with pytest.raises(ExecutionError) as excinfo:
        execute_with_correction(agent.conn, agent.schema, bad_sql, max_attempts=3)
    assert len(excinfo.value.attempts) <= 3
