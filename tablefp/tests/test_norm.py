"""Tests for SQL normalization and hashing expressions."""

import os
import pytest
import psycopg2

from tablefp.norm import (
    get_norm_expr,
    get_h64_expr,
    NUM_TEST_VECTORS,
    ABC_HASH_EXPECTED,
)


@pytest.fixture
def db_connection():
    """Create a test database connection."""
    dsn = os.environ.get("POSTGRES_DSN") or os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("POSTGRES_DSN or DATABASE_URL not set")
    conn = psycopg2.connect(dsn)
    yield conn
    conn.close()


class TestNormNum:
    """Test norm_num SQL expression with test vectors."""

    @pytest.mark.integration
    def test_norm_num_vectors(self, db_connection):
        norm_expr = get_norm_expr("num", "%s")
        for input_val, expected in NUM_TEST_VECTORS:
            query = f"SELECT {norm_expr}"
            with db_connection.cursor() as cur:
                cur.execute(query, (input_val,))
                result = cur.fetchone()[0]
                assert result == expected, f"Failed for input {input_val}: got {result}, expected {expected}"


class TestNormText:
    """Test norm_text SQL expression."""

    @pytest.mark.integration
    def test_norm_text_case(self, db_connection):
        norm_expr = get_norm_expr("text", "%s")
        query = f"SELECT {norm_expr}"
        with db_connection.cursor() as cur:
            cur.execute(query, ("HELLO WORLD",))
            assert cur.fetchone()[0] == "hello world"

    @pytest.mark.integration
    def test_norm_text_whitespace(self, db_connection):
        norm_expr = get_norm_expr("text", "%s")
        query = f"SELECT {norm_expr}"
        with db_connection.cursor() as cur:
            cur.execute(query, ("  hello   world  ",))
            assert cur.fetchone()[0] == "hello world"

    @pytest.mark.integration
    def test_norm_text_null(self, db_connection):
        norm_expr = get_norm_expr("text", "%s")
        query = f"SELECT {norm_expr}"
        with db_connection.cursor() as cur:
            cur.execute(query, ("   ",))
            assert cur.fetchone()[0] is None


class TestNormDate:
    """Test norm_date and norm_ts SQL expressions."""

    @pytest.mark.integration
    def test_norm_date(self, db_connection):
        norm_expr = get_norm_expr("date", "%s")
        query = f"SELECT {norm_expr}"
        with db_connection.cursor() as cur:
            cur.execute(query, ("2024-01-15",))
            assert cur.fetchone()[0] == "2024-01-15"

    @pytest.mark.integration
    def test_norm_ts(self, db_connection):
        norm_expr = get_norm_expr("ts", "%s")
        query = f"SELECT {norm_expr}"
        with db_connection.cursor() as cur:
            cur.execute(query, ("2024-01-15 14:30:45",))
            result = cur.fetchone()[0]
            assert result.startswith("2024-01-15")
            assert "T" in result


class TestNormBool:
    """Test bool normalization."""

    @pytest.mark.integration
    def test_norm_bool_true(self, db_connection):
        norm_expr = get_norm_expr("bool", "%s")
        query = f"SELECT {norm_expr}"
        with db_connection.cursor() as cur:
            cur.execute(query, (True,))
            assert cur.fetchone()[0] == "true"

    @pytest.mark.integration
    def test_norm_bool_false(self, db_connection):
        norm_expr = get_norm_expr("bool", "%s")
        query = f"SELECT {norm_expr}"
        with db_connection.cursor() as cur:
            cur.execute(query, (False,))
            assert cur.fetchone()[0] == "false"


class TestH64Hash:
    """Test 64-bit hash expression."""

    @pytest.mark.integration
    def test_hash_abc_stable(self, db_connection):
        h64 = get_h64_expr("text", "%s")
        query = f"SELECT {h64}"
        with db_connection.cursor() as cur:
            cur.execute(query, ("abc",))
            result = cur.fetchone()[0]
            assert result == ABC_HASH_EXPECTED, f"Hash of 'abc' changed: got {result}, expected {ABC_HASH_EXPECTED}"


class TestBuildH64Select:
    """Test build_h64_select function."""

    def test_build_h64_select_text(self):
        from tablefp.norm import build_h64_select
        expr = build_h64_select("text", "my_column")
        assert "md5" in expr
        assert "my_column" in expr
        assert "::bit(64)::bigint" in expr
