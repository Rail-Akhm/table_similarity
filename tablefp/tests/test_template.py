"""Tests for template loading and cell canonicalization."""

import pytest
from datetime import datetime, date

from tablefp.template import canonicalize_cell, infer_dtype_group


class TestCanonicalizeCell:
    """Test cell canonicalization."""

    def test_none(self):
        assert canonicalize_cell(None) is None

    def test_empty_string(self):
        assert canonicalize_cell("") is None

    def test_bool_true(self):
        assert canonicalize_cell(True) == "true"

    def test_bool_false(self):
        assert canonicalize_cell(False) == "false"

    def test_int(self):
        assert canonicalize_cell(42) == "42"

    def test_float_integer(self):
        assert canonicalize_cell(3.0) == "3"

    def test_float_decimal(self):
        result = canonicalize_cell(3.14159)
        assert result == "3.14159"

    def test_float_trailing_zeros(self):
        result = canonicalize_cell(1.500000)
        assert result == "1.5"

    def test_date(self):
        d = date(2024, 1, 15)
        assert canonicalize_cell(d) == "2024-01-15"

    def test_datetime(self):
        dt = datetime(2024, 1, 15, 14, 30, 45)
        assert canonicalize_cell(dt) == "2024-01-15T14:30:45"

    def test_string(self):
        assert canonicalize_cell("hello") == "hello"

    def test_string_whitespace(self):
        assert canonicalize_cell("  hello  ") == "hello"


class TestInferDtypeGroup:
    """Test dtype inference."""

    def test_all_numbers(self):
        values = ["1", "2.5", "100", "-5", "0.5"]
        assert infer_dtype_group(values) == "num"

    def test_all_dates(self):
        values = ["2024-01-15", "2024-02-20", "2024-03-10"]
        assert infer_dtype_group(values) == "date"

    def test_all_timestamps(self):
        values = ["2024-01-15T14:30:45", "2024-02-20T09:15:30"]
        assert infer_dtype_group(values) == "date"

    def test_mixed_text(self):
        values = ["hello", "world", "foo", "bar"]
        assert infer_dtype_group(values) == "text"

    def test_empty(self):
        assert infer_dtype_group([]) == "text"