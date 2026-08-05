"""Stage 3: Row verification."""

import logging
from typing import List, Optional, Tuple

import numpy as np
import psycopg2

from tablefp.hashing import row_similarity, trigram_sim
from tablefp.matcher import TableMatch
from tablefp.norm import get_h64_expr, get_norm_expr
from tablefp.store import ColumnRecord

logger = logging.getLogger(__name__)


def select_anchor(col_info: List[Tuple]) -> Tuple[int, bool]:
    """Choose the anchor column index and its fuzzy flag.

    col_info items are (col_match, tmpl_col, db_col, is_fuzzy). Prefers an
    exact-matched column with nd > 100; falls back to a fuzzy one with nd > 100;
    else index 0.

    Returns (anchor_index, is_fuzzy).
    """
    anchor_idx = 0
    anchor_exact_idx = None
    anchor_fuzzy_idx = None
    for idx, (_, _, db_col, is_fuzzy) in enumerate(col_info):
        if db_col and db_col.nd > 100:
            if not is_fuzzy:
                anchor_exact_idx = idx
                break
            elif anchor_fuzzy_idx is None:
                anchor_fuzzy_idx = idx

    if anchor_exact_idx is not None:
        return anchor_exact_idx, False
    elif anchor_fuzzy_idx is not None:
        return anchor_fuzzy_idx, True
    return anchor_idx, False


def verify_rows(
    conn: psycopg2.extensions.connection,
    match: TableMatch,
    template_columns: List,
    db_columns: List[ColumnRecord],
    limit: int = 100000,
    fuzzy_enabled: bool = False,
    min_containment: float = 0.3,
    verify_sim_threshold: float = 0.4,
) -> float:
    """Verify matched rows exist in the database.

    Returns ratio of template rows that match (0.0 to 1.0).
    """
    if not match.mapping:
        return 0.0

    # Classify each mapping as exact or fuzzy
    col_info: List[Tuple] = []
    for col_match in match.mapping:
        tmpl_col = template_columns[col_match.template_col_idx]
        db_col = next(
            (c for c in db_columns if c.column_name == col_match.db_column), None
        )
        is_fuzzy = (
            fuzzy_enabled
            and col_match.ngram_containment is not None
            and db_col is not None
            and db_col.dtype_group == "text"
        )
        col_info.append((col_match, tmpl_col, db_col, is_fuzzy))

    # Anchor selection: prefer exact-matched with nd > 100
    anchor_idx, anchor_is_fuzzy = select_anchor(col_info)

    anchor_match, anchor_tmpl_col, anchor_db_col, _ = col_info[anchor_idx]
    anchor_values = anchor_tmpl_col.row_norm_v[:limit]
    anchor_values = [v for v in anchor_values if v is not None]

    # Build SELECT for mapped columns: hash for exact, norm for fuzzy
    mapped_cols = []
    for col_match, tmpl_col, db_col, is_fuzzy in col_info:
        if db_col is None:
            mapped_cols.append(None)
            continue
        if is_fuzzy:
            norm_expr = get_norm_expr(db_col.dtype_group, db_col.column_name)
            mapped_cols.append({"type": "norm", "expr": norm_expr, "name": db_col.column_name})
        else:
            h64_expr = get_h64_expr(db_col.dtype_group, db_col.column_name)
            mapped_cols.append({"type": "hash", "expr": h64_expr, "name": db_col.column_name})

    select_parts = []
    for mc in mapped_cols:
        if mc is None:
            select_parts.append("NULL")
        else:
            select_parts.append(f'{mc["expr"]} AS "{mc["name"]}"')

    anchor_norm = get_norm_expr(anchor_db_col.dtype_group, anchor_db_col.column_name)

    # --- Fetch DB rows via exact anchor join (fast path) ---
    query = f"""
        SELECT DISTINCT {', '.join(select_parts)}
        FROM {anchor_db_col.schema}.{anchor_db_col.table_name} t
        JOIN unnest(%(anchor_vals)s::text[]) AS q(v)
          ON {anchor_norm} = q.v
        LIMIT {limit}
    """

    with conn.cursor() as cur:
        cur.execute(query, {"anchor_vals": anchor_values})
        db_rows = cur.fetchall()

    # Fuzzy fallback: if exact join found nothing and anchor is fuzzy, scan a subset
    if not db_rows and anchor_is_fuzzy:
        fuzzy_limit = min(limit, 10_000)
        fuzzy_query = f"""
            SELECT DISTINCT {anchor_norm} AS __anchor, {', '.join(select_parts)}
            FROM {anchor_db_col.schema}.{anchor_db_col.table_name}
            LIMIT {fuzzy_limit}
        """
        with conn.cursor() as cur:
            cur.execute(fuzzy_query)
            raw_rows = [(r[0], r[1:]) for r in cur.fetchall()]

        # Build template sigs
        tmpl_row_sigs = []
        n_rows = min(len(tc.row_hashes) for tc in template_columns) if template_columns else 0
        anchor_sig_pos = sum(1 for ci in col_info[:anchor_idx] if ci[2] is not None)

        for row_idx in range(min(n_rows, limit)):
            sig = []
            for _ci_idx, (_col_match, _tmpl_col, _db_col, _is_fuzzy) in enumerate(col_info):
                if _db_col is None:
                    continue
                if row_idx >= len(_tmpl_col.row_hashes):
                    sig.append(None)
                    continue
                if _is_fuzzy:
                    sig.append(("norm", _tmpl_col.row_norm_v[row_idx]))
                else:
                    sig.append(("hash", _tmpl_col.row_hashes[row_idx]))
            if any(s is not None and s[1] is not None for s in sig):
                tmpl_row_sigs.append(sig)

        if not tmpl_row_sigs:
            return 0.0

        n_cols = sum(1 for ci in col_info if ci[2] is not None)
        threshold_cols = max(1, int(0.8 * n_cols))
        matched_count = 0

        for tmpl_sig in tmpl_row_sigs:
            if anchor_sig_pos >= len(tmpl_sig):
                continue
            anchor_entry = tmpl_sig[anchor_sig_pos]
            if anchor_entry is None or anchor_entry[1] is None:
                continue
            anchor_sig_val = anchor_entry[1]

            for _db_row_idx, (db_anchor_norm, db_mapped) in enumerate(raw_rows):
                if db_anchor_norm is None:
                    continue
                if trigram_sim(anchor_sig_val, str(db_anchor_norm)) < verify_sim_threshold:
                    continue
                matches = 0
                pos = 0
                for sig_type, sig_val in tmpl_sig:
                    if pos >= len(db_mapped):
                        break
                    db_val = db_mapped[pos]
                    pos += 1
                    if sig_val is None or db_val is None:
                        continue
                    if sig_type == "hash":
                        if sig_val == db_val:
                            matches += 1
                    elif sig_type == "norm":
                        if row_similarity(sig_val, str(db_val)) >= verify_sim_threshold:
                            matches += 1
                if matches >= threshold_cols:
                    matched_count += 1
                    break

        return matched_count / len(tmpl_row_sigs)

    if not db_rows:
        return 0.0

    db_row_set = set(db_rows)

    # Build template row signatures
    tmpl_row_sigs = []
    n_rows = min(len(tc.row_hashes) for tc in template_columns) if template_columns else 0

    for row_idx in range(min(n_rows, len(anchor_values))):
        sig = []
        for col_info_idx, (col_match, tmpl_col, db_col, is_fuzzy) in enumerate(col_info):
            if db_col is None:
                continue
            if row_idx >= len(tmpl_col.row_hashes):
                sig.append(None)
                continue
            if is_fuzzy:
                sig.append(("norm", tmpl_col.row_norm_v[row_idx]))
            else:
                sig.append(("hash", tmpl_col.row_hashes[row_idx]))
        if any(s[1] is not None for s in sig):
            tmpl_row_sigs.append(sig)

    if not tmpl_row_sigs:
        return 0.0

    # Row matching
    n_cols = len(col_info)
    threshold_cols = max(1, int(0.8 * n_cols))
    matched_count = 0

    for tmpl_sig in tmpl_row_sigs:
        for db_row in db_row_set:
            matches = 0
            pos = 0
            for sig_type, sig_val in tmpl_sig:
                if pos >= len(db_row):
                    break
                db_val = db_row[pos]
                pos += 1

                if sig_val is None or db_val is None:
                    continue

                if sig_type == "hash":
                    if sig_val == db_val:
                        matches += 1
                elif sig_type == "norm":
                    if row_similarity(sig_val, str(db_val)) >= verify_sim_threshold:
                        matches += 1

            if matches >= threshold_cols:
                matched_count += 1
                break

    return matched_count / len(tmpl_row_sigs)
