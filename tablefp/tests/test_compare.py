import numpy as np

from tablefp.hashing import shared_trigram_spans
from tablefp.compare import _classify_pair, build_comparison
from tablefp.visualize import _highlight
from tablefp.matcher import TableMatch, ColumnMatch
from tablefp.store import ColumnRecord


class TestSharedTrigramSpans:
    def test_identical(self):
        spans = shared_trigram_spans("hello", "hello")
        # whole string covered
        merged = spans
        assert merged[0][0] == 0
        assert merged[-1][1] == len("hello")

    def test_no_overlap(self):
        assert shared_trigram_spans("abc", "xyz") == []

    def test_partial_overlap(self):
        # shared prefix "ha"->"hal" vs "hel"; common trigrams around 'llo'
        spans = shared_trigram_spans("hallo", "hello")
        # 'llo' region shared -> covers tail
        assert spans
        assert spans[-1][1] == 5

    def test_empty(self):
        assert shared_trigram_spans("", "abc") == []


class TestClassifyPair:
    def test_exact(self):
        r = _classify_pair("abc", "abc", "ABC", "abc", False, 3, 0.4)
        assert r["kind"] == "exact"
        assert r["src_spans"] == [(0, 3)]
        assert r["tgt_spans"] == [(0, 3)]

    def test_none_when_db_null(self):
        r = _classify_pair("abc", "abc", None, None, False, 3, 0.4)
        assert r["kind"] == "none"

    def test_fuzzy_hit(self):
        r = _classify_pair("hello world", "hello world", "hello werld", "hello werld", True, 3, 0.3)
        assert r["kind"] == "fuzzy"
        assert r["src_spans"]
        assert r["tgt_spans"]

    def test_fuzzy_below_threshold(self):
        r = _classify_pair("abc", "abc", "xyz", "xyz", True, 3, 0.4)
        assert r["kind"] == "none"

    def test_no_fuzzy_when_disabled(self):
        # different values, fuzzy not enabled -> none
        r = _classify_pair("hello", "hello", "hallo", "hallo", False, 3, 0.3)
        assert r["kind"] == "none"


class TestHighlight:
    def test_escapes_and_marks(self):
        out = _highlight("a<b&c", [(0, 3)], "exact")
        assert "&lt;" in out
        assert "&amp;" in out
        assert '<mark class="exact">' in out

    def test_no_spans(self):
        out = _highlight("a<b", [], "exact")
        assert out == "a&lt;b"
        assert "mark" not in out

    def test_none(self):
        out = _highlight(None, [], "exact")
        assert "—" in out


class _FakeCursor:
    """Routes queries: information_schema.columns vs the data query."""

    def __init__(self, info_cols, data_rows):
        self._info_cols = info_cols
        self._data_rows = data_rows
        self._result = []
        self.last_query = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, q, params=None):
        self.last_query = q
        if "information_schema.columns" in q:
            self._result = [(c,) for c in self._info_cols]
        else:
            self._result = self._data_rows

    def fetchall(self):
        return self._result


class _FakeConn:
    def __init__(self, info_cols, data_rows):
        self._info_cols = info_cols
        self._data_rows = data_rows
        self.last_cursor = None

    def cursor(self):
        self.last_cursor = _FakeCursor(self._info_cols, self._data_rows)
        return self.last_cursor


def _tmpl_col(name, values, norms, hashes, dtype="text", ngram=None):
    from tablefp.template import TemplateColumn
    return TemplateColumn(
        name=name,
        values=values,
        dtype_group=dtype,
        distinct_hashes=np.array(sorted(set(h for h in hashes if h is not None)), dtype=np.int64),
        row_hashes=hashes,
        row_norm_v=norms,
        ngram_hashes=ngram if ngram is not None else np.array([], dtype=np.int64),
    )


class _Tmpl:
    def __init__(self, columns):
        self.columns = columns


def _make_case():
    col = _tmpl_col("Name", ["Alice", "Bob"], ["alice", "bob"], [111, 222])
    tmpl = _Tmpl([col])
    db_col = ColumnRecord(
        schema="s", table_name="t", column_name="name",
        dtype_group="text", n=500, nd=500, npy_path="x",
    )
    match = TableMatch(
        schema="s", table_name="t", score=0.9,
        mapping=[ColumnMatch(
            template_col_idx=0, template_col_name="Name", db_column="name",
            containment=0.9, exact_containment=0.9, ngram_containment=None, nd=500,
        )],
    )
    return tmpl, db_col, match


def test_build_comparison_source_driven_matched_mode():
    # matched mode: source_columns == matched db cols; no information_schema call
    tmpl, db_col, match = _make_case()

    # Source-driven: data query returns rows FROM the DB table.
    # SELECT __anchor, raw::name, norm::name  (matched mode)
    # DB has two rows; 'alice' pairs with template row 0, 'carol' has no template.
    data_rows = [
        ("alice", "Alice", "alice"),
        ("carol", "Carol", "carol"),
    ]
    conn = _FakeConn(info_cols=[], data_rows=data_rows)

    data = build_comparison(conn, tmpl, [db_col], match, limit=500, columns_mode="matched")

    assert data["anchor"] == "name"
    assert data["source_columns"] == ["name"]
    assert data["target_columns"] == ["Name"]
    assert len(data["rows"]) == 2  # driven by DB rows

    # Cells: target first, then source (rows are payload-shaped {"m","c"})
    r0 = data["rows"][0]
    assert r0["m"] is True
    assert r0["c"][0] == {"v": "Alice", "k": "exact", "s": [[0, 5]]}   # target Name
    assert r0["c"][1] == {"v": "Alice", "k": "exact", "s": [[0, 5]]}   # source name

    # Second DB row 'Carol' has no template match
    r1 = data["rows"][1]
    assert r1["m"] is False
    assert r1["c"][0]["v"] is None
    assert r1["c"][1]["v"] == "Carol"


def test_build_comparison_all_columns_mode():
    # all mode: source columns come from information_schema (name + extra 'age')
    tmpl, db_col, match = _make_case()

    # information_schema returns two columns; data query returns raw::name, raw::age, norm::name
    data_rows = [
        # __anchor, raw::name, raw::age, norm::name
        ("alice", "Alice", "30", "alice"),
    ]
    conn = _FakeConn(info_cols=["name", "age"], data_rows=data_rows)

    data = build_comparison(conn, tmpl, [db_col], match, limit=500,
                            columns_mode="all", only_hit_columns=False)

    assert data["source_columns"] == ["name", "age"]
    r0 = data["rows"][0]
    # cells: target(Name), source(name), source(age)
    assert r0["c"][0]["v"] == "Alice"   # Name exact
    assert r0["c"][0]["k"] == "exact"
    assert r0["c"][1]["v"] == "Alice"   # name exact
    assert r0["c"][1]["k"] == "exact"
    assert r0["c"][2]["v"] == "30"      # age unmatched
    assert "k" not in r0["c"][2]


def test_build_comparison_matched_mode_all_template_columns():
    # matched mode + all_template_columns: source stays matched-only, but the
    # target side shows every template column even without matches
    col = _tmpl_col("Name", ["Alice", "Bob"], ["alice", "bob"], [111, 222])
    extra = _tmpl_col("Comment", ["hi", "yo"], ["hi", "yo"], [333, 444])
    tmpl = _Tmpl([col, extra])
    db_col = ColumnRecord(
        schema="s", table_name="t", column_name="name",
        dtype_group="text", n=500, nd=500, npy_path="x",
    )
    match = TableMatch(
        schema="s", table_name="t", score=0.9,
        mapping=[ColumnMatch(
            template_col_idx=0, template_col_name="Name", db_column="name",
            containment=0.9, exact_containment=0.9, ngram_containment=None, nd=500,
        )],
    )

    data_rows = [
        ("alice", "Alice", "alice"),
    ]
    conn = _FakeConn(info_cols=[], data_rows=data_rows)

    data = build_comparison(
        conn, tmpl, [db_col], match, limit=500,
        columns_mode="matched", all_template_columns=True, only_hit_columns=False,
    )

    assert data["source_columns"] == ["name"]
    assert data["target_columns"] == ["Name", "Comment"]
    r0 = data["rows"][0]
    # cells: target(Name), target(Comment), source(name)
    assert r0["c"][0] == {"v": "Alice", "k": "exact", "s": [[0, 5]]}
    assert r0["c"][1]["v"] == "hi"     # Comment: no match kind
    assert "k" not in r0["c"][1]
    assert r0["c"][2]["v"] == "Alice"


def test_build_comparison_uses_all_candidates():
    # Template col 'Name' matches TWO db columns (name + alt_name). Candidates
    # should surface both in matched mode, not just the assigned one.
    col = _tmpl_col("Name", ["Alice", "Bob"], ["alice", "bob"], [111, 222])
    tmpl = _Tmpl([col])
    db_cols = [
        ColumnRecord(schema="s", table_name="t", column_name="name",
                     dtype_group="text", n=500, nd=500, npy_path="x"),
        ColumnRecord(schema="s", table_name="t", column_name="alt_name",
                     dtype_group="text", n=500, nd=300, npy_path="y"),
    ]
    match = TableMatch(
        schema="s", table_name="t", score=0.9,
        mapping=[ColumnMatch(
            template_col_idx=0, template_col_name="Name", db_column="name",
            containment=0.9, exact_containment=0.9, ngram_containment=None, nd=500,
        )],
        candidates=[
            ColumnMatch(template_col_idx=0, template_col_name="Name", db_column="name",
                        containment=0.9, exact_containment=0.9, ngram_containment=None, nd=500),
            ColumnMatch(template_col_idx=0, template_col_name="Name", db_column="alt_name",
                        containment=0.6, exact_containment=0.6, ngram_containment=None, nd=300),
        ],
    )

    # data query: __anchor, raw::name, raw::alt_name, norm::name, norm::alt_name
    # both values match exactly so alt_name isn't filtered out
    data_rows = [("alice", "Alice", "Alice", "alice", "alice")]
    conn = _FakeConn(info_cols=[], data_rows=data_rows)

    data = build_comparison(conn, tmpl, db_cols, match, limit=500, columns_mode="matched")

    # BOTH matched db columns are shown, not just the assigned 'name'
    assert set(data["source_columns"]) == {"name", "alt_name"}
    assert len(data["matched_pairs"]) == 2
    row = data["rows"][0]
    # cells: target(Name), source(name), source(alt_name)
    assert row["c"][1]["v"] == "Alice"
    assert row["c"][1]["k"] == "exact"
    assert row["c"][2]["v"] == "Alice"
    assert row["c"][2]["k"] == "exact"


def test_dead_columns_with_zero_row_hits_are_filtered_out():
    # gruppa_raspredeleniya (no real hit) and division (long string) both pass
    # candidate_min_containment but only division should survive — gruppa has
    # zero actual cell matches.
    col = _tmpl_col("Name", ["Абаканское"], ["абаканское"], [111])
    tmpl = _Tmpl([col])
    db_cols = [
        ColumnRecord(schema="s", table_name="t", column_name="division",
                     dtype_group="text", n=500, nd=500, npy_path="x"),
        ColumnRecord(schema="s", table_name="t", column_name="gruppa",
                     dtype_group="text", n=500, nd=10, npy_path="y"),
    ]
    match = TableMatch(
        schema="s", table_name="t", score=0.9,
        mapping=[ColumnMatch(template_col_idx=0, template_col_name="Name",
                             db_column="division", containment=0.9,
                             exact_containment=0.9, ngram_containment=None, nd=500)],
        candidates=[
            ColumnMatch(template_col_idx=0, template_col_name="Name", db_column="division",
                        containment=0.9, exact_containment=0.9,
                        ngram_containment=0.02, nd=500),
            ColumnMatch(template_col_idx=0, template_col_name="Name", db_column="gruppa",
                        containment=0.006, exact_containment=0.0,
                        ngram_containment=0.006, nd=10),
        ],
    )

    data_rows = [
        ("абаканское", "Абаканское", "Прочие услуги", "абаканское", "прочие услуги"),
    ]
    conn = _FakeConn(info_cols=[], data_rows=data_rows)

    data = build_comparison(
        conn, tmpl, db_cols, match, limit=500,
        fuzzy_enabled=True, min_containment=0.3,
        ngram_size=3, verify_sim_threshold=0.4,
        columns_mode="matched",
    )

    # division survived (substring match), gruppa filtered (zero hits)
    assert set(data["source_columns"]) == {"division"}
    assert len(data["matched_pairs"]) == 1
    assert data["matched_pairs"][0]["source_col"] == "division"


def test_matched_pairs_expose_exact_and_ngram_containment():
    # Pairs must carry both containment values so the report can show
    # т:X% н:Y% for columns where both match types exist.
    from tablefp.visualize import _build_compare_payload

    col = _tmpl_col("Name", ["Alice"], ["alice"], [111])
    tmpl = _Tmpl([col])
    db_col = ColumnRecord(
        schema="s", table_name="t", column_name="name",
        dtype_group="text", n=500, nd=500, npy_path="x",
    )
    match = TableMatch(
        schema="s", table_name="t", score=0.9,
        mapping=[ColumnMatch(
            template_col_idx=0, template_col_name="Name", db_column="name",
            containment=0.9, exact_containment=0.9, ngram_containment=0.95, nd=500,
        )],
    )

    data_rows = [("alice", "Alice", "alice")]
    conn = _FakeConn(info_cols=[], data_rows=data_rows)

    data = build_comparison(conn, tmpl, [db_col], match, limit=500,
                            fuzzy_enabled=True, columns_mode="matched")

    pair = data["matched_pairs"][0]
    assert pair["exact_containment"] == 0.9
    assert pair["ngram_containment"] == 0.95

    legend = _build_compare_payload(data)["legend"][0]
    assert legend["exact"] == 0.9
    assert legend["ngram"] == 0.95


def test_only_hit_columns_filters_both_sides_in_all_mode():
    # all mode + only_hit_columns: source drops no-hit extras ('age'), target
    # drops template columns without a live pair ('Comment').
    col = _tmpl_col("Name", ["Alice"], ["alice"], [111])
    extra = _tmpl_col("Comment", ["hi"], ["hi"], [333])
    tmpl = _Tmpl([col, extra])
    db_col = ColumnRecord(
        schema="s", table_name="t", column_name="name",
        dtype_group="text", n=500, nd=500, npy_path="x",
    )
    match = TableMatch(
        schema="s", table_name="t", score=0.9,
        mapping=[ColumnMatch(
            template_col_idx=0, template_col_name="Name", db_column="name",
            containment=0.9, exact_containment=0.9, ngram_containment=None, nd=500,
        )],
    )

    # __anchor, raw::name, raw::age, norm::name
    data_rows = [("alice", "Alice", "30", "alice")]
    conn = _FakeConn(info_cols=["name", "age"], data_rows=data_rows)

    data = build_comparison(
        conn, tmpl, [db_col], match, limit=500,
        columns_mode="all", all_template_columns=True, only_hit_columns=True,
    )

    assert data["source_columns"] == ["name"]
    assert data["target_columns"] == ["Name"]
    r0 = data["rows"][0]
    assert [c["v"] for c in r0["c"]] == ["Alice", "Alice"]


def test_limit_none_omits_sql_limit_clause():
    # Default (unlimited) mode: the DB query must NOT contain a LIMIT clause.
    tmpl, db_col, match = _make_case()
    data_rows = [("alice", "Alice", "alice")]
    conn = _FakeConn(info_cols=[], data_rows=data_rows)

    build_comparison(conn, tmpl, [db_col], match, limit=None, columns_mode="matched")

    assert "LIMIT" not in conn.last_cursor.last_query


def test_limit_set_emits_sql_limit_clause():
    tmpl, db_col, match = _make_case()
    data_rows = [("alice", "Alice", "alice")]
    conn = _FakeConn(info_cols=[], data_rows=data_rows)

    build_comparison(conn, tmpl, [db_col], match, limit=500, columns_mode="matched")

    assert "LIMIT 500" in conn.last_cursor.last_query


def test_fuzzy_anchor_fallback_retains_fuzzy_only_rows():
    # Primary anchor is numeric 'id' (exact, not fuzzy-capable). A DB row whose
    # id is NOT in the template must still be aligned via the fuzzy-capable
    # 'name' anchor, so its text match is visible and --only-matched keeps it.
    id_col = _tmpl_col("id", [1, 2], ["1", "2"], [10, 20], dtype="num")
    name_col = _tmpl_col("name", ["Alice", "Bob"], ["alice", "bob"], [111, 222])
    tmpl = _Tmpl([id_col, name_col])
    db_cols = [
        ColumnRecord(schema="s", table_name="t", column_name="id",
                     dtype_group="num", n=3, nd=3, npy_path="x"),
        ColumnRecord(schema="s", table_name="t", column_name="name",
                     dtype_group="text", n=3, nd=3, npy_path="y"),
    ]
    match = TableMatch(
        schema="s", table_name="t", score=0.9,
        mapping=[
            ColumnMatch(template_col_idx=0, template_col_name="id", db_column="id",
                        containment=0.5, exact_containment=0.5,
                        ngram_containment=None, nd=3),
            ColumnMatch(template_col_idx=1, template_col_name="name", db_column="name",
                        containment=0.9, exact_containment=0.9,
                        ngram_containment=0.95, nd=3),
        ],
    )

    # SELECT order: __anchor(norm id), raw::id, raw::name, norm::id, norm::name
    data_rows = [
        ("1", "1", "Alice", "1", "alice"),   # id=1 -> exact anchor hit
        ("3", "3", "Bob", "3", "bob"),        # id=3 -> anchor miss; name fallback
    ]
    conn = _FakeConn(info_cols=["id", "name"], data_rows=data_rows)

    data = build_comparison(
        conn, tmpl, db_cols, match, limit=500,
        fuzzy_enabled=True, min_containment=0.3,
        ngram_size=3, verify_sim_threshold=0.4,
        columns_mode="all", only_matched=True,
    )

    # Both rows retained: row1's id didn't match the template, but 'name' did.
    assert len(data["rows"]) == 2
    r1 = data["rows"][1]
    assert r1["m"] is True
    # cells: target(id), target(name), source(id), source(name)
    assert "k" not in r1["c"][2]          # id: no match
    assert r1["c"][3]["k"] == "exact"     # name: exact


def test_exact_column_supports_row_level_fuzzy_matching():
    # If a text column is classified as 'exact' overall, row-level fuzzy matching
    # should still be active for rows that don't match exactly.
    col = _tmpl_col("Name", ["Абаканское", "Аганское"], ["абаканское", "аганское"], [111, 222])
    tmpl = _Tmpl([col])
    db_col = ColumnRecord(
        schema="s", table_name="t", column_name="name",
        dtype_group="text", n=500, nd=500, npy_path="x",
    )
    match = TableMatch(
        schema="s", table_name="t", score=0.9,
        mapping=[ColumnMatch(
            template_col_idx=0, template_col_name="Name", db_column="name",
            containment=0.9, exact_containment=0.9, ngram_containment=0.95, nd=500,
        )],
    )

    # DB row 0: 'Абаканское' matches 'Абаканское' exactly
    # DB row 1: 'Аганское_ч' differs slightly from template 'Аганское', triggering fuzzy
    data_rows = [
        ("абаканское", "Абаканское", "абаканское"),
        ("аганское_ч", "Аганское_ч", "аганское_ч"),
    ]
    conn = _FakeConn(info_cols=[], data_rows=data_rows)

    data = build_comparison(
        conn, tmpl, [db_col], match, limit=500,
        fuzzy_enabled=True, min_containment=0.3,
        ngram_size=3, verify_sim_threshold=0.4,
        columns_mode="matched"
    )

    # First row is exact
    r0 = data["rows"][0]
    assert r0["m"] is True
    assert r0["c"][1]["v"] == "Абаканское"  # source cell (after target)
    assert r0["c"][1]["k"] == "exact"

    # Second row is fuzzy (even though the column overall is exact)
    r1 = data["rows"][1]
    assert r1["m"] is True
    assert r1["c"][1]["v"] == "Аганское_ч"
    assert r1["c"][1]["k"] == "fuzzy"
    assert len(r1["c"][1]["s"]) > 0

