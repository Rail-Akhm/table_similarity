import json
from pathlib import Path

from tablefp.visualize import _render, generate_compare_index, generate_report


def test_report_shows_total_scanned_tables():
    sessions = [{"_file": "r.json", "template": "f.xlsx", "total_tables": 137}]
    rows = [{
        "file": "r.json", "template": "f.xlsx", "table": "dwh.orders",
        "score": 0.82, "verified": 0.9, "mapping": [], "unmatched": [],
        "candidates": [],
    }]
    html = _render(rows, sessions)
    assert "Просканировано" in html
    assert "<b>137</b>" in html


def test_report_total_scanned_defaults_to_dash_when_missing():
    # Old JSON files without total_tables
    sessions = [{"_file": "r.json", "template": "f.xlsx"}]
    rows = [{
        "file": "r.json", "template": "f.xlsx", "table": "dwh.orders",
        "score": 0.5, "verified": None, "mapping": [], "unmatched": [],
        "candidates": [],
    }]
    html = _render(rows, sessions)
    assert "Просканировано" in html
    assert "<b>—</b>" in html


def test_compare_index_lists_entries_sorted_with_links():
    entries = [
        {"file": "01_a_b.html", "table": "a.b", "score": 0.91,
         "verified": 0.8, "n_matched": 12, "n_rows": 20},
        {"file": "02_c_d.html", "table": "c.d", "score": 0.55,
         "verified": None, "n_matched": 3, "n_rows": 20},
    ]
    out = Path(__file__).parent / "_tmp_index.html"
    try:
        generate_compare_index(entries, str(out), template_name="f.xlsx")
        html = out.read_text(encoding="utf-8")
        assert "tablefp — отчёты сравнения" in html
        assert 'href="01_a_b.html"' in html
        assert 'href="02_c_d.html"' in html
        assert "a.b" in html and "c.d" in html
        assert "0.9100" in html
        assert "12 / 20" in html
    finally:
        out.unlink(missing_ok=True)


def test_generate_report_reads_total_tables_from_json(tmp_path):
    data = {"template": "f.xlsx", "total_tables": 42, "results": []}
    j = tmp_path / "r.json"
    j.write_text(json.dumps(data), encoding="utf-8")
    out = tmp_path / "report.html"
    generate_report([str(j)], str(out))
    html = out.read_text(encoding="utf-8")
    assert "Просканировано" in html
    assert "<b>42</b>" in html
