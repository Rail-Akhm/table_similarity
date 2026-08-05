"""Crawl information_schema for target tables/columns."""

import fnmatch
import logging
from dataclasses import dataclass
from typing import List, Optional, Set

import psycopg2

logger = logging.getLogger(__name__)


@dataclass
class ColumnInfo:
    """Metadata for a single column."""

    schema: str
    table_name: str
    column_name: str
    data_type: str
    dtype_group: str
    ordinal_position: int


TYPE_MAPPING = {
    "smallint": "num",
    "integer": "num",
    "int": "num",
    "bigint": "num",
    "numeric": "num",
    "decimal": "num",
    "real": "num",
    "double precision": "num",
    "text": "text",
    "character varying": "text",
    "varchar": "text",
    "character": "text",
    "char": "text",
    "date": "date",
    "timestamp": "ts",
    "timestamp without time zone": "ts",
    "timestamp with time zone": "ts",
    "timetz": "ts",
    "boolean": "bool",
    "uuid": "uuid",
}

EXCLUDED_TYPES = {
    "bytea", "json", "jsonb", "xml",
    "geometry", "geography", "array", "user-defined",
}


def get_dtype_group(data_type: str) -> Optional[str]:
    """Map PostgreSQL data type to dtype group."""
    return TYPE_MAPPING.get(data_type.lower().strip())


def _glob_to_sql_like(pattern: str) -> str:
    """Convert fnmatch glob to SQL LIKE pattern."""
    return pattern.replace("*", "%").replace("?", "_")


def expand_table_patterns(conn, patterns: List[str]) -> List[tuple]:
    """Expand patterns like 'schema_*.table*' or 'schema.*' to (schema, table) tuples.

    Supports fnmatch globs (* and ?) in both schema and table parts.
    """
    expanded = []
    seen = set()

    for pattern in patterns:
        parts = pattern.split(".")
        if len(parts) != 2:
            logger.warning(f"Invalid table pattern: {pattern} (expected 'schema.table')")
            continue

        schema_pat, table_pat = parts

        conditions = ["table_type = 'BASE TABLE'"]
        params = []

        if any(c in schema_pat for c in "*?"):
            conditions.append("table_schema LIKE %s")
            params.append(_glob_to_sql_like(schema_pat))
        else:
            conditions.append("table_schema = %s")
            params.append(schema_pat)

        if any(c in table_pat for c in "*?"):
            conditions.append("table_name LIKE %s")
            params.append(_glob_to_sql_like(table_pat))
        else:
            conditions.append("table_name = %s")
            params.append(table_pat)

        where = " AND ".join(conditions)
        query = f"SELECT table_schema, table_name FROM information_schema.tables WHERE {where}"

        with conn.cursor() as cur:
            cur.execute(query, params)
            for s, t in cur.fetchall():
                if fnmatch.fnmatch(s, schema_pat) and fnmatch.fnmatch(t, table_pat):
                    key = (s, t)
                    if key not in seen:
                        seen.add(key)
                        expanded.append(key)

    return expanded


def crawl_columns(
    conn,
    tables: List[str],
    exclude_columns: Optional[Set[str]] = None,
    exclude_column_patterns: Optional[List[str]] = None,
    dtype_groups: Optional[List[str]] = None,
) -> List[ColumnInfo]:
    """Crawl information_schema for configured tables."""
    if exclude_columns is None:
        exclude_columns = set()
    if exclude_column_patterns is None:
        exclude_column_patterns = []

    expanded_tables = expand_table_patterns(conn, tables)
    logger.debug(f"Expanded {len(tables)} patterns into {len(expanded_tables)} tables")

    columns = []
    for schema, table_name in expanded_tables:
        query = """
            SELECT column_name, data_type, ordinal_position
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
        """
        with conn.cursor() as cur:
            cur.execute(query, (schema, table_name))
            for col_name, data_type, ordinal_pos in cur.fetchall():
                full_name = f"{schema}.{table_name}.{col_name}"

                if full_name in exclude_columns:
                    continue

                if any(fnmatch.fnmatch(col_name, p) for p in exclude_column_patterns):
                    logger.debug(f"Skipping {full_name} (matches exclude pattern)")
                    continue

                dtype_group = get_dtype_group(data_type)
                if dtype_group is None or data_type.lower() in EXCLUDED_TYPES:
                    continue

                if dtype_groups and dtype_group not in dtype_groups:
                    continue

                columns.append(ColumnInfo(
                    schema=schema,
                    table_name=table_name,
                    column_name=col_name,
                    data_type=data_type,
                    dtype_group=dtype_group,
                    ordinal_position=ordinal_pos,
                ))

    return columns


def get_table_list(conn, pattern: str) -> List[tuple]:
    """Get list of tables matching a schema pattern (uses fnmatch globs)."""
    return expand_table_patterns(conn, [pattern])
