"""Matching stages: prefilter, containment, assignment, ranking."""

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
from scipy.optimize import linear_sum_assignment

from tablefp.store import ArtifactStore, ColumnRecord

logger = logging.getLogger(__name__)


@dataclass
class ColumnMatch:
    """A matched pair of template and DB columns."""

    template_col_idx: int
    template_col_name: str
    db_column: str
    containment: float
    exact_containment: float = 0.0
    ngram_containment: Optional[float] = None
    nd: int = 0


@dataclass
class TableMatch:
    """Match result for a single table."""

    schema: str
    table_name: str
    score: float
    mapping: List[ColumnMatch] = field(default_factory=list)
    unmatched_template_cols: List[int] = field(default_factory=list)
    verified_row_ratio: Optional[float] = None
    # All (template col, db col) pairs above threshold on exact OR ngram
    # containment, not just the 1:1 assigned ones. For diagnostics/reporting.
    candidates: List[ColumnMatch] = field(default_factory=list)


# Type compatibility matrix
TYPE_COMPAT = {
    ("num", "num"),
    ("date", "date"),
    ("ts", "ts"),
    ("text", "text"),
    ("bool", "bool"),
    ("uuid", "text"),  # UUID can match text
    ("text", "num"),   # Template text can match any (fallback)
    ("num", "text"),   # DB text can match template num (fallback)
}


def _ngram_similarity(
    tmpl_ngrams: np.ndarray,
    db_ngrams: np.ndarray,
    metric: str = "jaccard",
) -> float:
    """Trigram-set similarity between template and DB column.

    metric:
      - "jaccard": |A∩B| / |A∪B|. Symmetric; strongly penalizes a huge DB
        trigram set (anti-"sponge"). A large free-text column ranks low.
      - "coverage_weighted": coverage × jaccard = |A∩B|² / (|A| · |A∪B|).
        Rewards the raw number of template trigrams hit while still applying a
        size penalty, so a column with MANY hits (e.g. deposit names appearing
        as substrings of long strings) ranks above incidental trigram noise,
        but a genuine same-size match still wins.

    Uses sorted int64 hash arrays.
    """
    a = len(tmpl_ngrams)
    b = len(db_ngrams)
    if a == 0 or b == 0:
        return 0.0
    inter = int(np.isin(tmpl_ngrams, db_ngrams, assume_unique=True).sum())
    union = a + b - inter
    if union == 0:
        return 0.0
    jaccard = inter / union
    if metric == "coverage_weighted":
        coverage = inter / a
        return coverage * jaccard
    return jaccard


# Backwards-compatible alias (jaccard-only)
def _ngram_jaccard(tmpl_ngrams: np.ndarray, db_ngrams: np.ndarray) -> float:
    return _ngram_similarity(tmpl_ngrams, db_ngrams, "jaccard")


def is_compatible(tmpl_group: str, db_group: str) -> bool:
    """Check if two dtype groups are compatible."""
    return (tmpl_group, db_group) in TYPE_COMPAT or (tmpl_group == "text" and db_group != "bool")


def stage0_prefilter(
    template: "Template",
    db_columns: List[ColumnRecord],
) -> List[ColumnRecord]:
    """Stage 0: Prefilter DB columns by dtype compatibility and value range.

    Returns list of DISTINCT compatible DB columns (each DB column appears once,
    even if it is compatible with several template columns).
    """
    compatible = []
    seen = set()

    for tmpl_col in template.columns:
        for db_col in db_columns:
            key = (db_col.schema, db_col.table_name, db_col.column_name)
            if key in seen:
                continue

            # Check dtype compatibility
            if not is_compatible(tmpl_col.dtype_group, db_col.dtype_group):
                continue

            # For numeric columns, check range overlap
            if tmpl_col.dtype_group == "num" and db_col.dtype_group == "num":
                if tmpl_col.min_val is not None and tmpl_col.max_val is not None:
                    if db_col.min_val is not None and db_col.max_val is not None:
                        # Check if ranges overlap
                        if tmpl_col.max_val < db_col.min_val or tmpl_col.min_val > db_col.max_val:
                            continue

            # Skip if no distinct values
            if db_col.nd == 0:
                continue

            compatible.append(db_col)
            seen.add(key)

    return compatible


def stage1_containment(
    template: "Template",
    db_columns: List[ColumnRecord],
    store: ArtifactStore,
    fuzzy_enabled: bool = False,
    fuzzy_alpha: float = 0.8,
    fuzzy_metric: str = "jaccard",
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Stage 1: Build containment matrices.

    Returns (S, S_exact, S_ngram), each shape (n_template_cols, n_db_cols).
    S[i, j] = max(exact, alpha * ngram) for text columns; S_ngram holds NaN
    where no ngram containment was computed. Column arrays are loaded one at a
    time and released — no cross-column cache, so peak memory is one column's
    array instead of the whole table's.
    """
    n_tmpl = len(template.columns)
    n_db = len(db_columns)

    S = np.zeros((n_tmpl, n_db), dtype=np.float64)
    S_exact = np.zeros((n_tmpl, n_db), dtype=np.float64)
    S_ngram = np.full((n_tmpl, n_db), np.nan, dtype=np.float64)

    for j, db_col in enumerate(db_columns):
        db_hashes = store.load_hashes(db_col.npy_path)

        db_ngrams = None
        if fuzzy_enabled and db_col.dtype_group == "text" and db_col.ngrams_path:
            db_ngrams = store.load_ngrams(db_col.ngrams_path)

        for i, tmpl_col in enumerate(template.columns):
            if len(tmpl_col.distinct_hashes) == 0 or len(db_hashes) == 0:
                continue
            exact = float(np.isin(tmpl_col.distinct_hashes, db_hashes, assume_unique=True).mean())
            S_exact[i, j] = exact
            S[i, j] = exact

            if db_ngrams is not None and len(tmpl_col.ngram_hashes) > 0:
                ng = _ngram_similarity(tmpl_col.ngram_hashes, db_ngrams, fuzzy_metric)
                S_ngram[i, j] = ng
                S[i, j] = max(exact, fuzzy_alpha * ng)

    return S, S_exact, S_ngram


def stage2_assignment(
    S: np.ndarray,
    db_columns: List[ColumnRecord],
    min_containment: float = 0.3,
) -> Tuple[List[Tuple[int, int]], float, List[int], List[Tuple[int, int, float]]]:
    """Stage 2: Assignment and scoring.

    Uses Hungarian algorithm to find optimal column assignment.

    Returns:
        - pairs: List of (template_col_idx, db_col_idx) assignments
        - score: Overall table score
        - unmatched: List of template column indices not matched
        - details: List of (tmpl_idx, db_idx, containment) for all pairs
    """
    n_tmpl, n_db = S.shape

    if n_tmpl == 0 or n_db == 0:
        return [], 0.0, list(range(n_tmpl)), []

    # Weight by selectivity (log of distinct values)
    nd_array = np.array([col.nd for col in db_columns], dtype=np.float64)
    W = np.log2(nd_array + 1)

    # Use Hungarian algorithm to maximize S * W
    # Negate because linear_sum_assignment minimizes
    row_ind, col_ind = linear_sum_assignment(-(S * W))

    # Filter by minimum containment threshold
    pairs = []
    details = []
    matched_tmpl = set()

    for i, j in zip(row_ind, col_ind):
        if S[i, j] >= min_containment:
            pairs.append((i, j))
            matched_tmpl.add(i)
            details.append((i, j, S[i, j]))

    # Calculate score
    if not pairs:
        return [], 0.0, list(range(n_tmpl)), []

    weight_sum = sum(W[j] for _, j in pairs)
    if weight_sum == 0:
        return [], 0.0, list(range(n_tmpl)), []

    score = sum(S[i, j] * W[j] for i, j in pairs) / weight_sum

    # Coverage penalty
    coverage = len(pairs) / n_tmpl
    score *= coverage

    # Unmatched template columns
    unmatched = [i for i in range(n_tmpl) if i not in matched_tmpl]

    return pairs, score, unmatched, details


def match_table(
    template: "Template",
    db_columns: List[ColumnRecord],
    store: ArtifactStore,
    min_containment: float = 0.3,
    fuzzy_enabled: bool = False,
    fuzzy_alpha: float = 0.8,
    candidate_min_containment: Optional[float] = None,
    fuzzy_metric: str = "jaccard",
) -> Optional[TableMatch]:
    """Match template against a single table.

    Returns TableMatch if any columns match, None otherwise.

    candidate_min_containment controls the threshold for the reported
    `candidates` list only (defaults to min_containment). Lower it to surface
    weaker matches in the report without affecting scoring/assignment.
    """
    if candidate_min_containment is None:
        candidate_min_containment = min_containment
    # Stage 0: Prefilter
    compatible = stage0_prefilter(template, db_columns)

    if not compatible:
        return None

    # Stage 1: Containment matrices
    S, S_exact, S_ngram = stage1_containment(
        template, compatible, store, fuzzy_enabled, fuzzy_alpha, fuzzy_metric
    )

    # Stage 2: Assignment and score
    pairs, score, unmatched, details = stage2_assignment(S, compatible, min_containment)

    if not pairs:
        return None

    # Build mapping with exact/ng containment (read from matrices, no reloads)
    mapping = []
    for tmpl_idx, db_idx, _ in details:
        tmpl_col = template.columns[tmpl_idx]
        db_col = compatible[db_idx]
        ng = None if np.isnan(S_ngram[tmpl_idx, db_idx]) else float(S_ngram[tmpl_idx, db_idx])

        mapping.append(
            ColumnMatch(
                template_col_idx=tmpl_idx,
                template_col_name=tmpl_col.name,
                db_column=db_col.column_name,
                containment=S[tmpl_idx, db_idx],
                exact_containment=float(S_exact[tmpl_idx, db_idx]),
                ngram_containment=ng,
                nd=db_col.nd,
            )
        )

    # Build candidates: ALL (template col, db col) pairs where exact OR ngram
    # containment >= min_containment (not just the 1:1 assigned ones).
    candidates = []
    for i, tmpl_col in enumerate(template.columns):
        for j, db_col in enumerate(compatible):
            exact = float(S_exact[i, j])
            ng = None if np.isnan(S_ngram[i, j]) else float(S_ngram[i, j])
            best = max(exact, ng if ng is not None else 0.0)
            if best >= candidate_min_containment:
                candidates.append(
                    ColumnMatch(
                        template_col_idx=i,
                        template_col_name=tmpl_col.name,
                        db_column=db_col.column_name,
                        containment=best,
                        exact_containment=exact,
                        ngram_containment=ng,
                        nd=db_col.nd,
                    )
                )

    # Sort candidates: template col order, then best containment desc
    candidates.sort(key=lambda c: (c.template_col_idx, -c.containment))

    return TableMatch(
        schema=compatible[0].schema,
        table_name=compatible[0].table_name,
        score=score,
        mapping=mapping,
        unmatched_template_cols=unmatched,
        candidates=candidates,
    )