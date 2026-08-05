"""Tests for matching logic."""

import numpy as np
import pytest

from tablefp.matcher import is_compatible, match_table, stage0_prefilter, stage1_containment, stage2_assignment
from tablefp.store import ColumnRecord


class TestCompatibility:
    """Test dtype compatibility."""

    def test_same_type(self):
        assert is_compatible("num", "num")
        assert is_compatible("text", "text")
        assert is_compatible("date", "date")
        assert is_compatible("ts", "ts")
        assert is_compatible("bool", "bool")

    def test_uuid_to_text(self):
        assert is_compatible("uuid", "text")

    def test_incompatible(self):
        assert not is_compatible("num", "bool")
        assert not is_compatible("date", "num")

    def test_text_fallback(self):
        assert is_compatible("text", "num")
        assert is_compatible("text", "date")


class TestStage1Containment:
    """Test containment matrix calculation."""

    def test_perfect_containment(self):
        # All template hashes are in DB column
        tmpl_hashes = np.array([1, 2, 3], dtype=np.int64)
        db_hashes = np.array([1, 2, 3, 4, 5], dtype=np.int64)

        matches = np.isin(tmpl_hashes, db_hashes, assume_unique=True)
        containment = matches.mean()
        assert containment == 1.0

    def test_partial_containment(self):
        # Some template hashes in DB column
        tmpl_hashes = np.array([1, 2, 3, 4, 5], dtype=np.int64)
        db_hashes = np.array([3, 4, 5, 6, 7], dtype=np.int64)

        matches = np.isin(tmpl_hashes, db_hashes, assume_unique=True)
        containment = matches.mean()
        assert containment == 0.6

    def test_no_containment(self):
        # No template hashes in DB column
        tmpl_hashes = np.array([1, 2, 3], dtype=np.int64)
        db_hashes = np.array([4, 5, 6], dtype=np.int64)

        matches = np.isin(tmpl_hashes, db_hashes, assume_unique=True)
        containment = matches.mean()
        assert containment == 0.0


class TestStage2Assignment:
    """Test Hungarian algorithm assignment."""

    def test_perfect_match(self):
        # 3x3 identity-like matrix (perfect matches on diagonal)
        S = np.array([
            [0.95, 0.1, 0.1],
            [0.1, 0.95, 0.1],
            [0.1, 0.1, 0.95],
        ])
        db_columns = [
            ColumnRecord(schema="s", table_name="t", column_name="c1", dtype_group="num", n=100, nd=1000),
            ColumnRecord(schema="s", table_name="t", column_name="c2", dtype_group="num", n=100, nd=1000),
            ColumnRecord(schema="s", table_name="t", column_name="c3", dtype_group="num", n=100, nd=1000),
        ]

        pairs, score, unmatched, details = stage2_assignment(S, db_columns)

        assert len(pairs) == 3
        assert set(pairs) == {(0, 0), (1, 1), (2, 2)}
        assert len(unmatched) == 0

    def test_missing_column(self):
        # Template has 3 cols, DB has 6 cols, one template col has no match
        S = np.array([
            [0.9, 0.1, 0.1, 0.1, 0.1, 0.1],
            [0.1, 0.9, 0.1, 0.1, 0.1, 0.1],
            [0.05, 0.05, 0.05, 0.05, 0.05, 0.05],  # No good match
        ])
        db_columns = [
            ColumnRecord(schema="s", table_name="t", column_name=f"c{i}", dtype_group="num", n=100, nd=1000 if i < 2 else 500)
            for i in range(6)
        ]

        pairs, score, unmatched, details = stage2_assignment(S, db_columns, min_containment=0.3)

        assert len(pairs) == 2
        assert 2 in unmatched  # Third template col has no match

    def test_below_threshold(self):
        # All containments below threshold
        S = np.array([
            [0.2, 0.1],
            [0.1, 0.2],
        ])
        db_columns = [
            ColumnRecord(schema="s", table_name="t", column_name="c1", dtype_group="num", n=100, nd=1000),
            ColumnRecord(schema="s", table_name="t", column_name="c2", dtype_group="num", n=100, nd=1000),
        ]

        pairs, score, unmatched, details = stage2_assignment(S, db_columns, min_containment=0.3)

        assert len(pairs) == 0
        assert len(unmatched) == 2


class _TmplCol:
    def __init__(self, name, distinct_hashes, ngram_hashes=None, dtype_group="text"):
        self.name = name
        self.dtype_group = dtype_group
        self.distinct_hashes = np.array(sorted(distinct_hashes), dtype=np.int64)
        self.ngram_hashes = np.array(
            sorted(ngram_hashes) if ngram_hashes else [], dtype=np.int64
        )
        self.min_val = None
        self.max_val = None


class _Tmpl:
    def __init__(self, columns):
        self.columns = columns


class _FakeStore:
    """Returns hashes/ngrams keyed by npy_path/ngrams_path."""

    def __init__(self, hashes, ngrams=None):
        self._h = {k: np.array(sorted(v), dtype=np.int64) for k, v in hashes.items()}
        self._n = {k: np.array(sorted(v), dtype=np.int64) for k, v in (ngrams or {}).items()}

    def load_hashes(self, path):
        return self._h.get(path, np.array([], dtype=np.int64))

    def load_ngrams(self, path):
        return self._n.get(path, np.array([], dtype=np.int64))


class TestCandidates:
    """match_table should surface all matches per template column, not just assigned."""

    def test_multiple_db_columns_match_one_template_col(self):
        # Template col 'Name' hashes {1,2,3}. Two DB text cols both contain them.
        tmpl = _Tmpl([_TmplCol("Name", [1, 2, 3])])

        db_columns = [
            ColumnRecord(schema="s", table_name="t", column_name="high_card",
                         dtype_group="text", n=100, nd=30000, npy_path="p_high"),
            ColumnRecord(schema="s", table_name="t", column_name="real_col",
                         dtype_group="text", n=100, nd=300, npy_path="p_real"),
        ]
        store = _FakeStore(hashes={
            "p_high": [1, 2, 3, 4, 5, 6],   # exact contains all 3
            "p_real": [1, 2, 3],            # exact contains all 3
        })

        match = match_table(tmpl, db_columns, store, min_containment=0.3)
        assert match is not None
        # assignment picks exactly one
        assert len(match.mapping) == 1
        # candidates surface BOTH db columns for the single template col
        cand_cols = {c.db_column for c in match.candidates}
        assert cand_cols == {"high_card", "real_col"}
        for c in match.candidates:
            assert c.template_col_name == "Name"

    def test_fuzzy_candidate_included(self):
        # exact 0 but ngram high -> should still appear as candidate (fuzzy)
        tmpl = _Tmpl([_TmplCol("Name", [1, 2, 3], ngram_hashes=[10, 11, 12])])
        db_columns = [
            ColumnRecord(schema="s", table_name="t", column_name="fuzzycol",
                         dtype_group="text", n=100, nd=30000,
                         npy_path="p1", ngrams_path="ng1"),
        ]
        store = _FakeStore(
            hashes={"p1": [7, 8, 9]},          # 0 exact
            ngrams={"ng1": [10, 11, 12, 13]},  # 3/4 shared with template ngrams
        )
        match = match_table(tmpl, db_columns, store,
                            min_containment=0.3, fuzzy_enabled=True, fuzzy_alpha=0.8)
        assert match is not None
        assert len(match.candidates) == 1
        c = match.candidates[0]
        assert c.db_column == "fuzzycol"
        assert c.exact_containment == 0.0
        # Jaccard: |{10,11,12} ∩ {10,11,12,13}| / |union| = 3/4
        assert c.ngram_containment == 0.75

    def test_jaccard_penalizes_trigram_sponge(self):
        # Template has trigrams {1..10}.
        # sponge: huge trigram set that CONTAINS all template trigrams plus 1000
        #         unrelated ones -> high one-directional containment, low Jaccard.
        # relevant: same-size set as template, mostly overlapping -> high Jaccard.
        tmpl = _Tmpl([_TmplCol("Name", [900], ngram_hashes=list(range(1, 11)))])
        db_columns = [
            ColumnRecord(schema="s", table_name="t", column_name="sponge",
                         dtype_group="text", n=100, nd=30000,
                         npy_path="p_s", ngrams_path="ng_s"),
            ColumnRecord(schema="s", table_name="t", column_name="relevant",
                         dtype_group="text", n=100, nd=300,
                         npy_path="p_r", ngrams_path="ng_r"),
        ]
        store = _FakeStore(
            hashes={"p_s": [1], "p_r": [2]},  # ~0 exact for both
            ngrams={
                # sponge: contains all 10 template ngrams + 1000 others
                "ng_s": list(range(1, 11)) + list(range(1000, 2000)),
                # relevant: 8 of 10 template ngrams, similar-size set
                "ng_r": list(range(1, 9)) + [50, 51],
            },
        )
        match = match_table(tmpl, db_columns, store,
                            min_containment=0.3, fuzzy_enabled=True, fuzzy_alpha=0.8)
        assert match is not None
        cand = {c.db_column: c for c in match.candidates}

        # sponge Jaccard = 10 / (10 + 1010 - 10) = 10/1010 ≈ 0.0099  -> below threshold, dropped
        assert "sponge" not in cand
        # relevant Jaccard = 8 / (10 + 10 - 8) = 8/12 ≈ 0.667 -> kept
        assert "relevant" in cand
        assert abs(cand["relevant"].ngram_containment - 8 / 12) < 1e-9

        # And the assigned match is the relevant column, not the sponge
        assert match.mapping[0].db_column == "relevant"

    def test_candidate_min_containment_surfaces_weak_fuzzy(self):
        # 'good' is a strong exact match; 'weak' is a weak fuzzy match (Jaccard
        # ~0.1). With candidate_min_containment lowered, 'weak' appears in
        # candidates but is NOT the assigned match.
        tmpl = _Tmpl([_TmplCol("Name", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                               ngram_hashes=list(range(1, 11)))])
        db_columns = [
            ColumnRecord(schema="s", table_name="t", column_name="good",
                         dtype_group="text", n=100, nd=300,
                         npy_path="p_g", ngrams_path="ng_g"),
            ColumnRecord(schema="s", table_name="t", column_name="weak",
                         dtype_group="text", n=100, nd=30000,
                         npy_path="p_w", ngrams_path="ng_w"),
        ]
        store = _FakeStore(
            hashes={"p_g": list(range(1, 11)), "p_w": [999]},  # good exact=1.0, weak=0
            ngrams={
                "ng_g": list(range(1, 11)),
                # weak: contains all 10 template ngrams + 90 others -> Jaccard 10/100=0.1
                "ng_w": list(range(1, 11)) + list(range(100, 190)),
            },
        )
        # Default threshold: only 'good' is a candidate
        m1 = match_table(tmpl, db_columns, store,
                         min_containment=0.3, fuzzy_enabled=True, fuzzy_alpha=0.8)
        assert {c.db_column for c in m1.candidates} == {"good"}

        # Lowered candidate threshold: 'weak' also surfaces...
        m2 = match_table(tmpl, db_columns, store,
                         min_containment=0.3, fuzzy_enabled=True, fuzzy_alpha=0.8,
                         candidate_min_containment=0.05)
        assert {c.db_column for c in m2.candidates} == {"good", "weak"}
        # ...but the assigned/scored match is still 'good'
        assert m2.mapping[0].db_column == "good"

    def test_coverage_weighted_prioritizes_high_hit_columns(self):
        # bighits: shares MANY template trigrams but is a large set (like a long
        #          free-text 'division' column).
        # noise:   small set sharing FEW trigrams (incidental overlap).
        # Under jaccard, noise > bighits. Under coverage_weighted, bighits > noise.
        tmpl = _Tmpl([_TmplCol("Name", [900], ngram_hashes=list(range(1, 101)))])  # 100 trigrams
        db_columns = [
            ColumnRecord(schema="s", table_name="t", column_name="bighits",
                         dtype_group="text", n=100, nd=30000,
                         npy_path="p_b", ngrams_path="ng_b"),
            ColumnRecord(schema="s", table_name="t", column_name="noise",
                         dtype_group="text", n=100, nd=300,
                         npy_path="p_n", ngrams_path="ng_n"),
        ]
        store = _FakeStore(
            hashes={"p_b": [1], "p_n": [2]},
            ngrams={
                # bighits: shares 80/100 template trigrams, but set of 900
                "ng_b": list(range(1, 81)) + list(range(1000, 1820)),
                # noise: shares 15/100, small set of 30
                "ng_n": list(range(1, 16)) + list(range(500, 515)),
            },
        )

        mj = match_table(tmpl, db_columns, store, min_containment=0.0,
                         fuzzy_enabled=True, fuzzy_alpha=1.0, fuzzy_metric="jaccard")
        cj = {c.db_column: c.ngram_containment for c in mj.candidates}
        # jaccard: bighits = 80/920 ≈ 0.087; noise = 15/115 ≈ 0.130 -> noise higher
        assert cj["noise"] > cj["bighits"]

        mc = match_table(tmpl, db_columns, store, min_containment=0.0,
                         fuzzy_enabled=True, fuzzy_alpha=1.0, fuzzy_metric="coverage_weighted")
        cc = {c.db_column: c.ngram_containment for c in mc.candidates}
        # coverage_weighted: bighits = 0.8 * 0.087 = 0.070; noise = 0.15 * 0.130 = 0.020
        assert cc["bighits"] > cc["noise"]

    def test_no_duplicate_candidates_with_multiple_template_cols(self):
        # Two template text cols; one DB text col compatible with BOTH.
        # stage0_prefilter must not duplicate it, and candidates must be unique.
        tmpl = _Tmpl([
            _TmplCol("Code", [1, 2, 3]),
            _TmplCol("Name", [1, 2, 3]),
        ])
        db_columns = [
            ColumnRecord(schema="s", table_name="t", column_name="shared",
                         dtype_group="text", n=100, nd=500, npy_path="p"),
        ]
        store = _FakeStore(hashes={"p": [1, 2, 3]})

        compatible = stage0_prefilter(tmpl, db_columns)
        assert len(compatible) == 1  # not duplicated per template col

        match = match_table(tmpl, db_columns, store, min_containment=0.3)
        # candidates: (Code->shared) and (Name->shared), each ONCE
        seen = [(c.template_col_idx, c.db_column) for c in match.candidates]
        assert len(seen) == len(set(seen))  # no duplicates
        assert set(seen) == {(0, "shared"), (1, "shared")}