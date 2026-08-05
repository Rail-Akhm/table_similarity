"""Build column fingerprints."""

import fnmatch
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import List, Optional

import numpy as np
import psycopg2
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

from tablefp.catalog import ColumnInfo, crawl_columns
from tablefp.db import get_connection, get_cursor
from tablefp.hashing import build_ngram_hashes
from tablefp.norm import get_norm_expr, get_h64_expr
from tablefp.store import ArtifactStore, ColumnRecord

logger = logging.getLogger(__name__)


def index_column(
    conn: psycopg2.extensions.connection,
    store: ArtifactStore,
    column: ColumnInfo,
    skip_text_avg_len: int,
    force: bool = False,
    fuzzy_config: Optional[dict] = None,
) -> Optional[ColumnRecord]:
    """Index a single column.

    Returns ColumnRecord on success, None if skipped.
    """
    start_time = time.time()

    # Check if already indexed
    if not force:
        existing = store.get_column(column.schema, column.table_name, column.column_name)
        if existing:
            logger.debug(f"  skip {column.schema}.{column.table_name}.{column.column_name} (already indexed)")
            return existing

    norm_expr = get_norm_expr(column.dtype_group, column.column_name)
    h64_expr = get_h64_expr(column.dtype_group, column.column_name)

    logger.debug(f"Indexing {column.schema}.{column.table_name}.{column.column_name} ({column.dtype_group})")

    # Stage 1: Compute stats
    stats_query = f"""
        SELECT
            count(*) AS n,
            count(DISTINCT {norm_expr}) AS nd
        FROM {column.schema}.{column.table_name}
    """

    logger.debug(f"Computing stats...")

    # Add min/max/quantiles for num/date/ts
    if column.dtype_group in ("num", "date", "ts"):
        stats_query = f"""
            SELECT
                count(*) AS n,
                count(DISTINCT {norm_expr}) AS nd,
                min({norm_expr}),
                max({norm_expr}),
                percentile_disc(ARRAY[0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99])
                    WITHIN GROUP (ORDER BY {norm_expr})
            FROM {column.schema}.{column.table_name}
            WHERE {norm_expr} IS NOT NULL
        """

    # Add avg_len for text
    if column.dtype_group == "text":
        stats_query = f"""
            SELECT
                count(*) AS n,
                count(DISTINCT {norm_expr}) AS nd,
                avg(length({norm_expr})) AS avg_len
            FROM {column.schema}.{column.table_name}
        """

    try:
        with get_cursor(conn) as cur:
            cur.execute(stats_query)
            row = cur.fetchone()
    except Exception as e:
        raise RuntimeError(f"stats query failed: {e}") from e

    if not row:
        logger.warning(f"No data in {column.schema}.{column.table_name}.{column.column_name}")
        return None

    n, nd = int(row[0]), int(row[1])

    if nd == 0:
        logger.warning(f"Column {column.schema}.{column.table_name}.{column.column_name} has 0 distinct values, skipping")
        return None

    # Extract additional stats based on dtype
    min_val = max_val = quantiles = avg_len = None

    if column.dtype_group in ("num", "date", "ts"):
        raw_min, raw_max, raw_quantiles = row[2], row[3], row[4]
        if column.dtype_group == "num":
            # norm_expr returns canonical decimal text; cast to float for range checks
            try:
                min_val = float(raw_min) if raw_min is not None else None
                max_val = float(raw_max) if raw_max is not None else None
            except (ValueError, TypeError):
                min_val = max_val = None
        # date/ts: min/max are text; not used in range checks, keep None
        # quantiles: stored as JSON, coerce every element to str (avoid Decimal)
        quantiles = [str(q) for q in raw_quantiles] if raw_quantiles else None

    if column.dtype_group == "text" and len(row) > 2:
        avg_len = float(row[2]) if row[2] is not None else None
        if avg_len is not None and avg_len > skip_text_avg_len:
            logger.info(f"Skipping {column.schema}.{column.table_name}.{column.column_name} (avg_len={avg_len:.1f} > {skip_text_avg_len})")
            return None

    # Stage 2: Stream distinct hashes
    logger.debug(f"Streaming distinct values...")
    hashes_query = f"""
        SELECT DISTINCT {h64_expr}
        FROM {column.schema}.{column.table_name}
        WHERE {norm_expr} IS NOT NULL
    """

    hashes = []
    try:
        with get_cursor(conn) as cur:
            cur.execute(hashes_query)
            while True:
                rows = cur.fetchmany(50000)
                if not rows:
                    break
                for (h,) in rows:
                    hashes.append(h)
    except Exception as e:
        raise RuntimeError(f"hash query failed: {e}") from e

    # Sort and save
    hashes_array = np.sort(np.array(hashes, dtype=np.int64))

    npy_path = store.save_hashes(column.schema, column.table_name, column.column_name, hashes_array)

    # N-gram hashes for eligible text columns
    ngrams_path = ""
    if column.dtype_group == "text" and fuzzy_config and fuzzy_config.get("enabled"):
        fc = fuzzy_config
        eligible = (nd <= fc.get("max_nd", 2_000_000))
        fc_cols = fc.get("columns", []) or []
        if any(fnmatch.fnmatch(column.column_name, p) for p in fc_cols):
            eligible = True
        if eligible:
            logger.debug(f"Building n-grams for {column.schema}.{column.table_name}.{column.column_name}")
            norm_query = f"""
                SELECT DISTINCT {norm_expr}
                FROM {column.schema}.{column.table_name}
                WHERE {norm_expr} IS NOT NULL
            """
            norm_values = []
            try:
                with get_cursor(conn) as cur:
                    cur.execute(norm_query)
                    while True:
                        rows = cur.fetchmany(50000)
                        if not rows:
                            break
                        for (v,) in rows:
                            norm_values.append(v)
            except Exception as e:
                raise RuntimeError(f"n-gram query failed: {e}") from e

            ngrams_array = build_ngram_hashes(norm_values, n=fc.get("ngram_size", 3))
            ngrams_path = store.save_ngrams(
                column.schema, column.table_name, column.column_name, ngrams_array
            )
            logger.debug(f"  {len(ngrams_array)} n-gram hashes")

    # Create record
    elapsed = time.time() - start_time
    record = ColumnRecord(
        schema=column.schema,
        table_name=column.table_name,
        column_name=column.column_name,
        dtype_group=column.dtype_group,
        n=n,
        nd=nd,
        min_val=min_val,
        max_val=max_val,
        quantiles=quantiles,
        avg_len=avg_len,
        npy_path=npy_path,
        ngrams_path=ngrams_path,
        indexed_at=datetime.utcnow().isoformat(),
    )

    store.upsert_column(record)

    logger.info(
        f"  {column.schema}.{column.table_name}.{column.column_name}"
        f"  n={n:,}  nd={nd:,}  {elapsed:.1f}s"
    )

    return record


def index_tables(
    dsn: str,
    tables: List[str],
    exclude_columns: List[str],
    exclude_column_patterns: List[str],
    store: "ArtifactStore",
    max_workers: int,
    skip_text_avg_len: int,
    force: bool = False,
    fuzzy_config: Optional[dict] = None,
    dtype_groups: Optional[List[str]] = None,
):
    """Index all configured tables."""
    conn = get_connection(dsn)
    columns = crawl_columns(conn, tables, set(exclude_columns), exclude_column_patterns, dtype_groups)
    logger.info(f"Found {len(columns)} columns to index")

    if not columns:
        logger.warning("No columns found to index")
        conn.close()
        return

    # Index in parallel
    failed = []
    completed = 0

    def worker(column: ColumnInfo):
        """Worker function that creates its own connection."""
        worker_conn = get_connection(dsn)
        try:
            return index_column(
                worker_conn,
                store,
                column,
                skip_text_avg_len,
                force,
                fuzzy_config,
            )
        finally:
            worker_conn.close()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(worker, col): col for col in columns}

        with logging_redirect_tqdm():
            pbar = tqdm(total=len(columns), desc="Indexing", unit="col", dynamic_ncols=True)
            for future in as_completed(futures):
                col = futures[future]
                try:
                    result = future.result()
                    if result is None:
                        failed.append(col)
                    completed += 1
                except Exception as e:
                    logger.error(f"FAIL  {col.schema}.{col.table_name}.{col.column_name}: {e}")
                    failed.append(col)
                pbar.set_postfix_str(f"{col.table_name}.{col.column_name}", refresh=False)
                pbar.update(1)
            pbar.close()

    conn.close()

    if failed:
        logger.warning(f"Failed to index {len(failed)} columns")
    else:
        logger.info(f"Successfully indexed all {len(columns)} columns")