import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

EXAMPLES_DIR = os.path.join(os.path.dirname(__file__), "..", "examples")
DB_PATH = os.path.join(EXAMPLES_DIR, "store.db")


@pytest.fixture(scope="session", autouse=True)
def ensure_example_db():
    if not os.path.exists(DB_PATH):
        sys.path.insert(0, EXAMPLES_DIR)
        import build_db

        build_db.build()
    yield


@pytest.fixture()
def agent():
    from text_to_sql.agent import TextToSQLAgent

    a = TextToSQLAgent(DB_PATH)
    yield a
    a.close()


@pytest.fixture()
def schema():
    from text_to_sql.schema import load_schema

    return load_schema(DB_PATH)
