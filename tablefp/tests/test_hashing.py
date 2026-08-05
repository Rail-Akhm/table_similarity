from tablefp.hashing import h64, ngrams, trigram_sim, row_similarity, build_ngram_hashes
import numpy as np


class TestH64:
    def test_equals_sql_hash(self):
        from tablefp.norm import ABC_HASH_EXPECTED
        assert h64("abc") == ABC_HASH_EXPECTED


class TestNgrams:
    def test_normal(self):
        ng = ngrams("abc")
        assert " ab" in ng
        assert "abc" in ng
        assert "bc " in ng

    def test_short_string(self):
        ng = ngrams("ab")
        assert len(ng) == 2
        assert " ab" in ng
        assert "ab " in ng

    def test_single_char(self):
        ng = ngrams("x")
        assert ng == {" x "}

    def test_empty(self):
        ng = ngrams("")
        assert ng == {"  "}


class TestTrigramSim:
    def test_identical(self):
        assert trigram_sim("hello", "hello") == 1.0

    def test_one_char_diff(self):
        sim = trigram_sim("hello", "hallo")
        assert sim > 0.2

    def test_completely_different(self):
        sim = trigram_sim("hello", "world")
        assert sim < 0.4

    def test_empty_strings(self):
        assert trigram_sim("", "") == 1.0


class TestRowSimilarity:
    def test_substring_containment(self):
        # 'Аленкинское' (short template) vs 'ТМСК Внеш_тран_Аленкинское' (long DB)
        # Jaccard trigram_sim is low (approx 0.37) but row_similarity (containment) is high (approx 0.9)
        tmpl = "аленкинское"
        db = "тмск внеш_тран_аленкинское"
        assert trigram_sim(tmpl, db) < 0.4
        assert row_similarity(tmpl, db) >= 0.8

    def test_unrelated(self):
        assert row_similarity("аленкинское", "тмск заказы revex ален") < 0.4


class TestBuildNgramHashes:
    def test_single_value(self):
        arr = build_ngram_hashes(["abc"])
        assert isinstance(arr, np.ndarray)
        assert arr.dtype == np.int64
        assert len(arr) > 0
