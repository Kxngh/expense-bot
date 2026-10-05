"""Run the whole suite against Postgres too:  TEST_DATABASE_URL=postgresql://... pytest -q"""
import os

import pytest

from app import config, db, reports

PG_URL = os.getenv("TEST_DATABASE_URL", "")
TABLES = ["expenses", "budgets", "users", "user_categories", "reports", "processed_messages"]


@pytest.fixture(autouse=True)
def _backend(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", PG_URL)
    reports._handled.clear()
    if PG_URL:
        with db.conn() as c:
            c.execute("DROP TABLE IF EXISTS " + ", ".join(TABLES))
    yield


@pytest.fixture(autouse=True, scope="session")
def _close_pool():
    yield
    db.close()
