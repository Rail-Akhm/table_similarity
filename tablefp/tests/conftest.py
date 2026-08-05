"""Pytest configuration and fixtures."""

import os
import pytest


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "integration: marks tests as integration tests requiring a database"
    )


@pytest.fixture
def db_dsn():
    """Get database connection string from environment.

    Requires POSTGRES_DSN or DATABASE_URL environment variable.
    """
    return os.environ.get("POSTGRES_DSN") or os.environ.get("DATABASE_URL")


@pytest.fixture
def skip_if_no_db(db_dsn):
    """Skip test if no database connection string is available."""
    if not db_dsn:
        pytest.skip("No database connection string available (set POSTGRES_DSN or DATABASE_URL)")