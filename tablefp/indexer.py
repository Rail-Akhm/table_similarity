"""Build column fingerprints."""

import fnmatch
import json
import logging
import os
import shutil
import struct
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
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

# --------------------------------------------------------------------------- #
# Low-memory streaming helpers
# --------------------------------------------------------------------------- #


def _write_npy_header(f, shape, dtype=np.dtype("<i8")):
    """Write .npy v1.0 header to an open file.

    Produces exactly the same format as ``np.save`` so the result is loadable
    with ``np.load(path, mmap_mode="r")``.
    """
    # .npy header MUST be a Python literal (evaluated by ast.literal_eval),
    # NOT JSON.  ``False`` / ``True``, not ``false`` / ``true``.
    header = (
        "{" +
        f"'descr': '{dtype.str}', 'fortran_order': False, 'shape': {shape}"
        + "}"
    )
    header_bytes = header.encode("ascii")

    # Total fixed overhead before header: magic(6) + version(2) + header_len(2) = 10.
    # The spec says the whole header (10 + len(header_padded)) must align data to
    # a 16-byte boundary.  We follow numpy's convention: pad header to make
    # 10 + len(header_padded) ≡ 0 (mod 16).
    target = len(header_bytes) + 1  # +1 for the mandatory '\n'
    remainder = (10 + target) % 16
    pad = (16 - remainder) % 16
    padded = header_bytes + b" " * pad + b"\n"

    f.write(b"\x93NUMPY")          # magic
    f.write(b"\x01")               # major version
    f.write(b"\x00")               # minor version
    f.write(struct.pack("<H", len(padded)))
    f.write(padded)


def _stream_sorted_hashes_to_npy(cursor, nd, npy_path):
    """Stream sorted int64 hashes from a cursor directly into a ``.npy`` file.

    The cursor MUST already be executing a query that returns rows ordered by
    the hash column (``ORDER BY 1``).  Hashes are written to disk in small
    batches — peak memory is O(batch_size), not O(nd).
    """
    batch_size = 100_000
    written = 0

    with open(npy_path, "wb") as f:
        _write_npy_header(f, (nd,), np.dtype("<i8"))

        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            batch = np.array([r[0] for r in rows], dtype=np.int64)
            f.write(batch.tobytes())
            written += len(batch)

    # Safety: if row count changed between stats and hash query, fix the header
    if written != nd:
        logger.warning(
            f"Hash count mismatch: expected {nd}, got {written}. "
            f"Fixing header (this is harmless)."
        )
        # Re-write header with correct shape
        data = np.memmap(npy_path, dtype=np.int64, mode="r", offset=0)
        actual = np.array(data[:written], dtype=np.int64)
        del data
        np.save(npy_path, actual)


def _build_ngrams_batched(norm_query, conn, ngram_size, ngrams_path):
    """Build n-gram hashes in batches and merge to a single ``.ngrams.npy``.

    Each batch fetches at most 100 000 normalised values, builds sorted
    deduplicated n-gram hashes via ``build_ngram_hashes``, and saves the
    batch to a temporary file.  Afterwards all batches are merged with a
    k-way heap merge + dedup that streams the result to disk.
    """
    BATCH = 100_000
    tmp_dir = Path(ngrams_path).parent / ".ngram_tmp_" + Path(ngrams_path).name
    os.makedirs(tmp_dir, exist_ok=True)

    batch_files = []
    try:
        # --- Phase 1: build per-batch .npy files ---
        with get_cursor(conn) as cur:
            cur.execute(norm_query)
            batch_idx = 0
            while True:
                rows = cur.fetchmany(BATCH)
                if not rows:
                    break
                values = [r[0] for r in rows]
                ngram_hashes = build_ngram_hashes(values, n=ngram_size)
                if len(ngram_hashes) == 0:
                    continue
                bf = tmp_dir / f"b_{batch_idx:06d}.npy"
                np.save(str(bf), ngram_hashes)
                batch_files.append(str(bf))
                batch_idx += 1
                logger.debug(f"  ngram batch {batch_idx}: {len(values):,} values → {len(ngram_hashes):,} hashes")

        if not batch_files:
            np.save(ngrams_path, np.array([], dtype=np.int64))
            return

        # --- Phase 2: k-way heap merge + dedup → final file ---
        _merge_sorted_dedup(batch_files, ngrams_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _merge_sorted_dedup(temp_files, output_path):
    """k-way merge of sorted int64 .npy files, deduplicating across batches."""
    import heapq

    arrays = []
    positions = []
    for f in temp_files:
        arr = np.load(f, mmap_mode="r")
        if len(arr) > 0:
            arrays.append(arr)
            positions.append(0)

    if not arrays:
        np.save(output_path, np.array([], dtype=np.int64))
        return

    # Initial heap: (value, array_index)
    heap = []
    for i in range(len(arrays)):
        heapq.heappush(heap, (int(arrays[i][0]), i))
        positions[i] = 1

    out_batches = []
    out_buf = []
    last_val = None

    while heap:
        val, i = heapq.heappop(heap)
        if last_val is None or val != last_val:
            out_buf.append(val)
            last_val = val
            if len(out_buf) >= 50_000:
                out_batches.append(np.array(out_buf, dtype=np.int64))
                out_buf = []

        if positions[i] < len(arrays[i]):
            next_val = int(arrays[i][positions[i]])
            heapq.heappush(heap, (next_val, i))
            positions[i] += 1

    if out_buf:
        out_batches.append(np.array(out_buf, dtype=np.int64))

    result = np.concatenate(out_batches) if out_batches else np.array([], dtype=np.int64)
    np.save(output_path, result)
    logger.debug(f"  merged {len(temp_files)} batches → {len(result):,} unique n-gram hashes")


def index_column(
    conn: psycopg2.extensions.connection,
    store: ArtifactStore,
    column: ColumnInfo,
    skip_text_avg_len: int,
    force: bool = False,
    fuzzy_config: Optional[dict] = None,
    low_memory: bool = False,
) -> Optional[ColumnRecord]:
    """Index a single column.

    Returns ColumnRecord on success, None if skipped.

    When *low_memory* is True the function uses DB-side ``ORDER BY`` +
    direct-to-disk streaming for hashes and batch-processing for n-grams,
    keeping peak memory O(batch_size) instead of O(nd).
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

    # Stage 2: Stream / collect distinct hashes
    npy_path = ""
    ngrams_path = ""

    if low_memory:
        # ── low-memory path: DB-side ORDER BY + direct-to-disk streaming ──
        logger.debug(f"Streaming distinct hashes (low-memory, ORDER BY in DB)...")
        hashes_query = f"""
            SELECT DISTINCT {h64_expr}
            FROM {column.schema}.{column.table_name}
            WHERE {norm_expr} IS NOT NULL
            ORDER BY 1
        """

        # For ArtifactStore (local) we write the final .npy directly.
        # For DBArtifactStore we stream to a temp file, then mmap-load and
        # pass through store.save_hashes() (BYTEA serialisation is unavoidable).
        if isinstance(store, ArtifactStore):
            table_dir = Path(store.index_dir) / column.schema / column.table_name
            os.makedirs(table_dir, exist_ok=True)
            npy_path = str(table_dir / f"{column.column_name}.npy")
            npy_tmp = None
        else:
            _tmp = tempfile.NamedTemporaryFile(suffix=".npy", delete=False)
            npy_path = _tmp.name
            _tmp.close()
            npy_tmp = npy_path  # will be cleaned up after store.save_hashes

        try:
            with get_cursor(conn) as cur:
                cur.execute(hashes_query)
                _stream_sorted_hashes_to_npy(cur, nd, npy_path)
        except Exception as e:
            raise RuntimeError(f"low-memory hash streaming failed: {e}") from e

        # For DB store: mmap the temp .npy and save through the store
        if npy_tmp is not None:
            try:
                hashes_mmap = np.load(npy_path, mmap_mode="r")
                npy_path = store.save_hashes(
                    column.schema, column.table_name, column.column_name, hashes_mmap
                )
            finally:
                try:
                    os.unlink(npy_tmp)
                except OSError:
                    pass

        # N-gram hashes — batch-processed (low-memory)
        if column.dtype_group == "text" and fuzzy_config and fuzzy_config.get("enabled"):
            fc = fuzzy_config
            eligible = (nd <= fc.get("max_nd", 2_000_000))
            fc_cols = fc.get("columns", []) or []
            if any(fnmatch.fnmatch(column.column_name, p) for p in fc_cols):
                eligible = True
            if eligible:
                logger.debug(f"Building n-grams (batched) for {column.schema}.{column.table_name}.{column.column_name}")
                norm_query = f"""
                    SELECT DISTINCT {norm_expr}
                    FROM {column.schema}.{column.table_name}
                    WHERE {norm_expr} IS NOT NULL
                """
                if isinstance(store, ArtifactStore):
                    table_dir = Path(store.index_dir) / column.schema / column.table_name
                    os.makedirs(table_dir, exist_ok=True)
                    ngrams_path = str(table_dir / f"{column.column_name}.ngrams.npy")
                    try:
                        _build_ngrams_batched(norm_query, conn, fc.get("ngram_size", 3), ngrams_path)
                    except Exception as e:
                        raise RuntimeError(f"batched n-gram build failed: {e}") from e
                else:
                    # DB store: batch to temp, mmap, save through store
                    _tmp2 = tempfile.NamedTemporaryFile(suffix=".ngrams.npy", delete=False)
                    _tmp2.close()
                    try:
                        _build_ngrams_batched(norm_query, conn, fc.get("ngram_size", 3), _tmp2.name)
                        ngrams_mmap = np.load(_tmp2.name, mmap_mode="r")
                        ngrams_path = store.save_ngrams(
                            column.schema, column.table_name, column.column_name, ngrams_mmap
                        )
                    finally:
                        try:
                            os.unlink(_tmp2.name)
                        except OSError:
                            pass
    else:
        # ── normal path: collect in Python, sort, save (original behaviour) ──
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
    low_memory: bool = False,
):
    """Index all configured tables."""
    conn = get_connection(dsn)
    columns = crawl_columns(conn, tables, set(exclude_columns), exclude_column_patterns, dtype_groups)
    logger.info(f"Found {len(columns)} columns to index")

    if not columns:
        logger.warning("No columns found to index")
        conn.close()
        return

    # Low-memory mode: single worker avoids concurrent allocations
    if low_memory:
        if max_workers > 1:
            logger.info(
                f"Low-memory mode: limiting workers from {max_workers} to 1 "
                f"(set low_memory: false to restore parallelism)"
            )
        max_workers = 1
        logger.info(
            f"Low-memory mode enabled: DB-side ORDER BY + disk streaming, "
            f"batched n-gram processing"
        )

    # Index in parallel
    failed = []
    completed = 0

    # Pre-compute table grouping for progress display
    _table_order: List[tuple] = list(dict.fromkeys(
        (c.schema, c.table_name) for c in columns
    ))
    _col_per_table: dict = {}
    for c in columns:
        key = (c.schema, c.table_name)
        _col_per_table[key] = _col_per_table.get(key, 0) + 1
    total_tables = len(_table_order)

    # Track table completion: table is "done" when all its columns are done
    _table_done: set = set()
    _table_col_done: dict = {}

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
                low_memory=low_memory,
            )
        finally:
            worker_conn.close()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(worker, col): col for col in columns}

        with logging_redirect_tqdm():
            pbar = tqdm(
                total=len(columns),
                desc="Tables 0/0",
                unit="col",
                dynamic_ncols=True,
                bar_format=(
                    "{desc:>24s}  {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} "
                    "[{elapsed}<{remaining}, {rate_fmt}{postfix}]"
                ),
            )
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

                # Update table-level progress
                key = (col.schema, col.table_name)
                _table_col_done[key] = _table_col_done.get(key, 0) + 1
                if _table_col_done[key] >= _col_per_table.get(key, 0):
                    _table_done.add(key)

                tables_done = len(_table_done)
                pbar.set_description(f"Tables {tables_done}/{total_tables}")
                pbar.set_postfix_str(
                    f"{col.schema}.{col.table_name}.{col.column_name}",
                    refresh=False,
                )
                pbar.update(1)
            pbar.close()

    conn.close()

    if failed:
        logger.warning(f"Failed to index {len(failed)} columns")
    else:
        logger.info(f"Successfully indexed all {len(columns)} columns")