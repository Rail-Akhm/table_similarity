"""Build a side-by-side row comparison for a single matched table.

Source-driven: the DB table's rows are shown on the left, and the matching
template row (aligned via the anchor column) is shown on the right. Each matched
cell is classified exact/fuzzy/none; for fuzzy text cells the shared trigram
character spans are computed for highlighting on both sides.
"""

import logging
from typing import Dict, List, Optional, Tuple

import psycopg2

from tablefp.hashing import row_similarity, shared_trigram_spans, trigram_sim
from tablefp.matcher import TableMatch
from tablefp.norm import get_norm_expr
from tablefp.store import ColumnRecord
from tablefp.verify import select_anchor

logger = logging.getLogger(__name__)


def _spans_for(raw: Optional[str], norm: Optional[str], other_norm: str, ngram_size: int) -> List[tuple]:
    """Shared trigram spans of `norm` vs `other_norm`, mapped onto `raw`."""
    if raw is None or norm is None:
        return []
    norm_spans = shared_trigram_spans(norm, other_norm, ngram_size)
    if not norm_spans:
        return []
    offset = raw.lower().find(norm)
    if offset >= 0:
        return [(s + offset, e + offset) for s, e in norm_spans]
    return [(s, min(e, len(raw))) for s, e in norm_spans if s < len(raw)]


def _classify_pair(
    tmpl_raw: Optional[str],
    tmpl_norm: Optional[str],
    db_raw: Optional[str],
    db_norm: Optional[str],
    is_fuzzy: bool,
    ngram_size: int,
    verify_sim_threshold: float,
) -> dict:
    """Classify a matched cell pair and compute highlight spans for both sides.

    Returns {kind, src_spans, tgt_spans} where src_spans index into db_raw and
    tgt_spans index into tmpl_raw.
    """
    if db_raw is None or tmpl_norm is None or db_norm is None:
        return {"kind": "none", "src_spans": [], "tgt_spans": []}

    if tmpl_norm == db_norm:
        return {
            "kind": "exact",
            "src_spans": [(0, len(db_raw))],
            "tgt_spans": [(0, len(tmpl_raw))] if tmpl_raw else [],
        }

    if is_fuzzy and row_similarity(tmpl_norm, db_norm, ngram_size) >= verify_sim_threshold:
        return {
            "kind": "fuzzy",
            "src_spans": _spans_for(db_raw, db_norm, tmpl_norm, ngram_size),
            "tgt_spans": _spans_for(tmpl_raw, tmpl_norm, db_norm, ngram_size),
        }

    return {"kind": "none", "src_spans": [], "tgt_spans": []}


def build_comparison(
    conn: psycopg2.extensions.connection,
    template,
    db_columns: List[ColumnRecord],
    match: TableMatch,
    limit: Optional[int] = 500,
    fuzzy_enabled: bool = False,
    min_containment: float = 0.3,
    ngram_size: int = 3,
    verify_sim_threshold: float = 0.4,
    columns_mode: str = "all",
    extra_target_columns: Optional[list] = None,
    only_matched: bool = False,
    all_template_columns: bool = False,
    only_hit_columns: bool = True,
) -> dict:
    """Produce a source-driven comparison structure for rendering.

    Left = DB source rows (all columns or matched-only per columns_mode), right =
    matching template row aligned via the anchor column.
    """
    # Use ALL candidate matches (every db column whose exact OR fuzzy containment
    # is above threshold), not just the single 1:1 assigned mapping. Falls back to
    # mapping if candidates are unavailable (older results).
    source_matches = match.candidates if match.candidates else match.mapping

    # Classify each candidate as exact or fuzzy (same rule as verify_rows)
    col_info: List[Tuple] = []
    for col_match in source_matches:
        tmpl_col = template.columns[col_match.template_col_idx]
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

    col_info = [ci for ci in col_info if ci[2] is not None]
    empty = {
        "table": f"{match.schema}.{match.table_name}",
        "score": float(match.score),
        "verified_row_ratio": None,
        "anchor": None,
        "columns_mode": columns_mode,
        "source_columns": [],
        "target_columns": [],
        "matched_pairs": [],
        "rows": [],
    }
    if not col_info:
        return empty

    anchor_idx, anchor_is_fuzzy = select_anchor(col_info)
    _, anchor_tmpl_col, anchor_db_col, _ = col_info[anchor_idx]

    # Secondary fuzzy anchor: a fuzzy-capable column used to align rows that the
    # primary (exact) anchor can't match. Lets --only-matched / row matching
    # retain rows that match only fuzzily (e.g. text matches but the numeric
    # anchor differs). Only needed when the primary anchor is not fuzzy-capable.
    fuzzy_anchor_tmpl_col = None
    fuzzy_anchor_db_col = None
    if fuzzy_enabled and not anchor_is_fuzzy:
        best = None  # (tmpl_col, db_col); pick highest-nd fuzzy-capable column
        for cm, tc, dc, is_fuzzy in col_info:
            if is_fuzzy and dc is not None:
                if best is None or dc.nd > best[1].nd:
                    best = (tc, dc)
        if best is not None:
            fuzzy_anchor_tmpl_col, fuzzy_anchor_db_col = best

    # Map: db column name -> matched pair info. Candidates may contain several db
    # columns per template column; each db column is shown once (a db column
    # matching multiple template cols keeps its highest-containment pairing).
    db_to_pair = {}
    tmpl_name_to_pair = {}
    matched_pairs = []
    for col_match, tmpl_col, db_col, is_fuzzy in col_info:
        pair = {
            "source_col": db_col.column_name,
            "target_col": tmpl_col.name,
            "kind": "fuzzy" if col_match.exact_containment < min_containment else "exact",
            "containment": float(col_match.containment),
            "exact_containment": float(col_match.exact_containment),
            "ngram_containment": (
                float(col_match.ngram_containment)
                if col_match.ngram_containment is not None else None
            ),
            "is_anchor": db_col.column_name == anchor_db_col.column_name,
            "tmpl_col": tmpl_col,
            "db_col": db_col,
            "is_fuzzy": is_fuzzy,
        }
        existing = db_to_pair.get(db_col.column_name)
        if existing is not None and existing["containment"] >= pair["containment"]:
            continue  # keep the stronger pairing for this db column
        if existing is not None:
            matched_pairs = [p for p in matched_pairs if p["source_col"] != db_col.column_name]
        db_to_pair[db_col.column_name] = pair
        # first (strongest, since candidates are sorted desc) pairing per template col
        tmpl_name_to_pair.setdefault(tmpl_col.name, pair)
        matched_pairs.append(pair)

    # --- Determine source (DB) columns to display ---
    schema, table_name = anchor_db_col.schema, anchor_db_col.table_name
    if columns_mode == "all":
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                (schema, table_name),
            )
            source_columns = [r[0] for r in cur.fetchall()]
        if not source_columns:  # fallback
            source_columns = [p["source_col"] for p in matched_pairs]
    else:
        source_columns = [p["source_col"] for p in matched_pairs]

    # --- Determine target (template) columns to display ---
    extra_target_columns = extra_target_columns or []
    if columns_mode == "all" or all_template_columns:
        target_columns = [tc.name for tc in template.columns]
        target_columns += [tc.name for tc in extra_target_columns]
    else:
        target_columns = list(dict.fromkeys(p["target_col"] for p in matched_pairs))

    # --- Fetch source rows: raw cols + normalized expr for matched cols + norm anchor ---
    anchor_norm_expr = get_norm_expr(anchor_db_col.dtype_group, anchor_db_col.column_name)
    select_parts = [f'{anchor_norm_expr} AS "__anchor"']
    for name in source_columns:
        select_parts.append(f'"{name}" AS "raw::{name}"')
    # normalized value for matched db columns (for classification)
    norm_index = {}
    for p in matched_pairs:
        dc = p["db_col"]
        norm_index[dc.column_name] = len(select_parts)
        select_parts.append(
            f'{get_norm_expr(dc.dtype_group, dc.column_name)} AS "norm::{dc.column_name}"'
        )

    # Fuzzy anchor position: reuse the norm column already added for matched
    # pairs when possible, else append a dedicated one.
    fuzzy_anchor_pos = None
    if fuzzy_anchor_db_col is not None:
        fuzzy_anchor_pos = norm_index.get(fuzzy_anchor_db_col.column_name)
        if fuzzy_anchor_pos is None:
            fuzzy_anchor_pos = len(select_parts)
            select_parts.append(
                f'{get_norm_expr(fuzzy_anchor_db_col.dtype_group, fuzzy_anchor_db_col.column_name)} '
                f'AS "__fuzzy_anchor"'
            )

    limit_clause = f"LIMIT {limit}" if limit is not None else ""
    query = f"""
        SELECT {', '.join(select_parts)}
        FROM {schema}.{table_name} t
        {limit_clause}
    """
    with conn.cursor() as cur:
        cur.execute(query)
        db_rows = cur.fetchall()

    # Column index offsets within the fetched row
    # row[0] = __anchor; raw cols start at 1
    raw_base = 1
    raw_idx = {name: raw_base + i for i, name in enumerate(source_columns)}

    # --- Build template index by anchor normalized value ---
    tmpl_by_anchor: Dict[str, int] = {}
    tmpl_anchor_vals: List[tuple] = []  # (norm_val, row_idx) for fuzzy scan
    for row_idx, av in enumerate(anchor_tmpl_col.row_norm_v):
        if av is not None:
            if av not in tmpl_by_anchor:
                tmpl_by_anchor[av] = row_idx
            if anchor_is_fuzzy:
                tmpl_anchor_vals.append((av, row_idx))

    # Template values for the secondary fuzzy anchor (for fallback alignment)
    tmpl_fuzzy_anchor_vals: List[tuple] = []
    if fuzzy_anchor_tmpl_col is not None:
        for row_idx, av in enumerate(fuzzy_anchor_tmpl_col.row_norm_v):
            if av is not None:
                tmpl_fuzzy_anchor_vals.append((av, row_idx))

    # Precompute template column lookup by display name (incl. display-only extras)
    tmpl_col_by_name = {tc.name: tc for tc in template.columns}
    for tc in extra_target_columns:
        tmpl_col_by_name.setdefault(tc.name, tc)

    def _fuzzy_best(val, vals):
        best_idx, best_sim = None, 0.0
        for av, row_idx in vals:
            sim = trigram_sim(av, str(val))
            if sim > best_sim:
                best_sim = sim
                best_idx = row_idx
        return best_idx if best_sim >= verify_sim_threshold else None

    def _find_tmpl_row(db_anchor_val, db_fuzzy_anchor_val=None):
        if db_anchor_val is not None:
            if not anchor_is_fuzzy:
                idx = tmpl_by_anchor.get(db_anchor_val)
                if idx is not None:
                    return idx
            else:
                idx = _fuzzy_best(db_anchor_val, tmpl_anchor_vals)
                if idx is not None:
                    return idx
        # Fuzzy fallback on the secondary anchor (primary exact anchor missed)
        if db_fuzzy_anchor_val is not None and tmpl_fuzzy_anchor_vals:
            idx = _fuzzy_best(db_fuzzy_anchor_val, tmpl_fuzzy_anchor_vals)
            if idx is not None:
                return idx
        return None

    rows_out = []
    for db_row in db_rows:
        anchor_val = db_row[0]
        fuzzy_anchor_val = db_row[fuzzy_anchor_pos] if fuzzy_anchor_pos is not None else None
        tmpl_row_idx = _find_tmpl_row(anchor_val, fuzzy_anchor_val)

        # Classify each matched pair once; store src/tgt spans keyed by db col name
        pair_cls: Dict[str, dict] = {}
        if tmpl_row_idx is not None:
            for name, pair in db_to_pair.items():
                tc = pair["tmpl_col"]
                t_raw = tc.values[tmpl_row_idx] if tmpl_row_idx < len(tc.values) else None
                t_raw = None if t_raw is None else str(t_raw)
                t_norm = tc.row_norm_v[tmpl_row_idx] if tmpl_row_idx < len(tc.row_norm_v) else None
                db_raw = db_row[raw_idx[name]] if name in raw_idx else None
                db_raw = None if db_raw is None else str(db_raw)
                db_norm = db_row[norm_index[name]]
                db_norm = None if db_norm is None else str(db_norm)
                pair_cls[name] = _classify_pair(
                    t_raw, t_norm, db_raw, db_norm,
                    pair["is_fuzzy"], ngram_size, verify_sim_threshold,
                )

        # source cells (payload-shaped: v=value, k=kind, s=spans)
        src_cells = []
        for name in source_columns:
            raw = db_row[raw_idx[name]]
            raw = None if raw is None else str(raw)
            cls = pair_cls.get(name)
            cell = {"v": raw}
            if cls and cls["kind"] != "none":
                cell["k"] = cls["kind"]
                if cls["src_spans"]:
                    cell["s"] = [[int(a), int(b)] for a, b in cls["src_spans"]]
            src_cells.append(cell)

        # target cells
        tgt_cells = []
        for name in target_columns:
            tc = tmpl_col_by_name.get(name)
            t_raw = None
            kind, spans = "none", []
            if tc is not None and tmpl_row_idx is not None:
                t_raw = tc.values[tmpl_row_idx] if tmpl_row_idx < len(tc.values) else None
                t_raw = None if t_raw is None else str(t_raw)
                pair = tmpl_name_to_pair.get(name)
                if pair is not None:
                    cls = pair_cls.get(pair["db_col"].column_name)
                    if cls:
                        kind = cls["kind"]
                        spans = cls["tgt_spans"]
            cell = {"v": t_raw}
            if kind != "none":
                cell["k"] = kind
                if spans:
                    cell["s"] = [[int(a), int(b)] for a, b in spans]
            tgt_cells.append(cell)

        matched = tmpl_row_idx is not None and any(c.get("k") in ("exact", "fuzzy") for c in src_cells)
        rows_out.append({"m": matched, "c": tgt_cells + src_cells})

    # Drop matched pairs that produced ZERO row-level hits (e.g. a column with
    # 0.6% ngram column-level containment but no cell actually matching)
    live_src_cols = set()
    col_hits: Dict[str, int] = {}
    n_tgt = len(target_columns)
    for r in rows_out:
        for cell, col in zip(r["c"][n_tgt:], source_columns):
            if cell.get("k") in ("exact", "fuzzy"):
                live_src_cols.add(col)
                col_hits[col] = col_hits.get(col, 0) + 1
    n_rows_total = len(rows_out)
    if live_src_cols:
        orig_src, orig_tgt = source_columns, target_columns
        matched_pairs = [p for p in matched_pairs if p["source_col"] in live_src_cols]
        db_to_pair = {k: v for k, v in db_to_pair.items() if v["source_col"] in live_src_cols}
        if columns_mode == "matched" or only_hit_columns:
            source_columns = [c for c in source_columns if c in live_src_cols]
            if not all_template_columns or only_hit_columns:
                target_columns = list(dict.fromkeys(p["target_col"] for p in matched_pairs))
            # Strip dead cells from each row so body matches the filtered headers
            src_keep = set(source_columns)
            tgt_keep = set(target_columns)
            for r in rows_out:
                tgt = [c for c, col in zip(r["c"][:n_tgt], orig_tgt) if col in tgt_keep]
                src = [c for c, col in zip(r["c"][n_tgt:], orig_src) if col in src_keep]
                r["c"] = tgt + src

    public_pairs = [
        {k: p[k] for k in ("source_col", "target_col", "kind", "containment", "is_anchor",
                           "exact_containment", "ngram_containment")}
        | {"hit_rows": col_hits.get(p["source_col"], 0), "total_rows": n_rows_total}
        for p in matched_pairs
    ]

    return {
        "table": f"{match.schema}.{match.table_name}",
        "score": float(match.score),
        "verified_row_ratio": (
            float(match.verified_row_ratio)
            if match.verified_row_ratio is not None else None
        ),
        "anchor": anchor_db_col.column_name,
        "columns_mode": columns_mode,
        "source_columns": source_columns,
        "target_columns": target_columns,
        "matched_pairs": public_pairs,
        "only_matched": only_matched,
        "rows": [r for r in rows_out if r.get("m")] if only_matched else rows_out,
    }
