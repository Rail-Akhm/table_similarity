"""SQL normalization and hashing expressions.

All normalization and hashing MUST go through these SQL expressions.
Never normalize/hash values in Python.
"""

from typing import Optional

_TEMPLATES = {
    "text": "nullif(lower(btrim(regexp_replace({x}::text, '\\s+', ' ', 'g'))), '')",
    "num": "CASE WHEN {x} IS NULL THEN NULL ELSE regexp_replace(COALESCE(NULLIF(rtrim(rtrim(round({x}::numeric, 6)::text, '0'), '.'), ''), '0'), '^-(0)$', '\\1') END",
    "date": "to_char({x}, 'YYYY-MM-DD')",
    "ts": "to_char({x}, 'YYYY-MM-DD\"T\"HH24:MI:SS')",
    "bool": "CASE WHEN {x} THEN 'true' ELSE 'false' END",
    "uuid": "nullif(lower(btrim(regexp_replace({x}::text, '\\s+', ' ', 'g'))), '')",
}


def get_norm_expr(dtype_group: str, column: str) -> str:
    """Return the SQL normalization expression for a dtype group.

    Args:
        dtype_group: One of 'text', 'num', 'date', 'ts', 'bool', 'uuid'
        column: Column name or expression to normalize

    Returns:
        SQL expression string with column name substituted
    """
    template = _TEMPLATES.get(dtype_group, _TEMPLATES["text"])
    return template.format(x=column)


def get_h64_expr(dtype_group: str, column: str) -> str:
    """Return the SQL 64-bit hash expression for a dtype group.

    Wraps the normalized value in md5 hash, PG 9.4 compatible.
    """
    norm = get_norm_expr(dtype_group, column)
    return "('x' || substr(md5({norm}), 1, 16))::bit(64)::bigint".format(norm=norm)


def build_h64_select(dtype_group: str, column: str) -> str:
    """Build a SELECT expression for hashing a column."""
    return get_h64_expr(dtype_group, column)


# Test vectors for norm_num
NUM_TEST_VECTORS = [
    (1.500000, "1.5"),
    (100, "100"),
    (0.0, "0"),
    (-0.0000001, "0"),
    (-2.50, "-2.5"),
    (1234.567890123, "1234.56789"),
]

# Expected hash of 'abc'
# md5('abc') = 900150983cd24fb0d6963f7d28e17f72
# First 8 hex bytes as signed int64 (matches PG ::bigint):
ABC_HASH_EXPECTED = -8070080442485551184