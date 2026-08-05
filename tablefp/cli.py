"""Command-line interface for tablefp."""

import json
import logging
import os
import sys
from datetime import datetime
from typing import Optional

import click

from tablefp.config import Config
from tablefp.indexer import index_tables
from tablefp.template import load_template
from tablefp.matcher import match_table
from tablefp.store import ArtifactStore, create_store
from tablefp.db import get_connection
from tablefp.verify import verify_rows

# Configure root logger first
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)


@click.group()
@click.version_option(version="0.1.0")
def cli():
    """tablefp — fuzzy search of DB tables by xlsx template."""
    pass


@cli.command()
@click.option("--config", required=True, help="Path to config.yaml")
@click.option("--tables", multiple=True, help="Table patterns to index (can be repeated)")
@click.option("--force", is_flag=True, help="Re-index already indexed columns")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
def index(config: str, tables: tuple, force: bool, verbose: bool):
    """Build index for configured tables."""
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    cfg = Config.from_yaml(config)
    if tables:
        cfg.tables = list(tables)

    click.echo(f"Config: {config}")
    click.echo(f"Tables: {', '.join(cfg.tables)}")
    if cfg.store_type == "db":
        click.echo(f"Store:  database ({cfg.storage_dsn or cfg.dsn})")
    else:
        click.echo(f"Store:  local ({cfg.index_dir})")
    click.echo()

    store = create_store(cfg)

    index_tables(
        dsn=cfg.dsn,
        tables=cfg.tables,
        exclude_columns=cfg.exclude_columns,
        exclude_column_patterns=cfg.exclude_column_patterns,
        store=store,
        max_workers=cfg.max_workers,
        skip_text_avg_len=cfg.skip_text_avg_len,
        force=force,
        fuzzy_config=cfg.fuzzy,
        dtype_groups=cfg.dtype_groups,
        max_memory_mb=cfg.max_memory_mb,
    )


@cli.command()
@click.argument("template")
@click.option("--config", required=True, help="Path to config.yaml")
@click.option("--top", default=5, help="Number of top results to show")
@click.option("--no-verify", is_flag=True, help="Skip row verification")
@click.option("--sheet", help="Sheet name (default: first sheet)")
@click.option("--no-header", is_flag=True, help="Template has no header row")
@click.option("--min-distinct", type=int, help="Override min_template_distinct threshold")
@click.option("--out", help="Output JSON file path")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
@click.option("--fuzzy/--no-fuzzy", default=None, help="Enable/disable fuzzy matching (default: from config)")
def search(template: str, config: str, top: int, no_verify: bool, sheet: str, no_header: bool, min_distinct: Optional[int], out: str, verbose: bool, fuzzy: Optional[bool]):
    """Search for matching tables using a template file."""
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    cfg = Config.from_yaml(config)
    store = create_store(cfg)

    # CLI flag overrides config
    fuzzy_enabled = cfg.fuzzy.get("enabled", False) if fuzzy is None else fuzzy
    min_tmpl_distinct = cfg.min_template_distinct if min_distinct is None else min_distinct

    click.echo(f"Template: {template}")
    click.echo(f"Index:    {cfg.index_dir if cfg.store_type == 'local' else 'database'}")
    if fuzzy_enabled:
        click.echo(f"Fuzzy:    enabled (metric={cfg.fuzzy.get('metric', 'jaccard')}, alpha={cfg.fuzzy.get('alpha', 0.8)}, ngram_size={cfg.fuzzy.get('ngram_size', 3)})")
    click.echo()

    conn = get_connection(cfg.dsn)

    try:
        tmpl = load_template(
            template, conn,
            sheet_name=sheet,
            header=None if not no_header else False,
            min_template_distinct=min_tmpl_distinct,
            fuzzy_enabled=fuzzy_enabled,
            ngram_size=cfg.fuzzy.get("ngram_size", 3),
        )
        click.echo(f"Columns loaded from template: {len(tmpl.columns)}")
    except Exception as e:
        click.echo(f"ERROR: Failed to load template: {e}", err=True)
        sys.exit(1)
    finally:
        conn.close()

    if not tmpl.columns:
        click.echo("ERROR: No valid columns in template", err=True)
        sys.exit(1)

    all_columns = store.list_columns()
    click.echo(f"Indexed columns in store: {len(all_columns)}")

    tables = {}
    for col in all_columns:
        key = (col.schema, col.table_name)
        if key not in tables:
            tables[key] = []
        tables[key].append(col)

    click.echo(f"Tables to match: {len(tables)}")
    click.echo()

    results = []
    for (schema, table_name), db_columns in tables.items():
        click.echo(f"Matching {schema}.{table_name} ... ", nl=False)

        class MatchWithDB:
            def __init__(self, match, db_cols):
                self._db_columns = db_cols
                self.schema = match.schema
                self.table_name = match.table_name
                self.score = match.score
                self.mapping = match.mapping
                self.unmatched_template_cols = match.unmatched_template_cols
                self.verified_row_ratio = match.verified_row_ratio
                self.candidates = match.candidates

        match = match_table(
            tmpl, db_columns, store,
            min_containment=cfg.min_containment,
            fuzzy_enabled=fuzzy_enabled,
            fuzzy_alpha=cfg.fuzzy.get("alpha", 0.8),
            candidate_min_containment=cfg.candidate_min_containment,
            fuzzy_metric=cfg.fuzzy.get("metric", "jaccard"),
        )
        if match:
            matched = MatchWithDB(match, db_columns)
            pairs_str = ", ".join(
                f"{m.template_col_name} -> {m.db_column} {m.containment:.1%}"
                for m in match.mapping
            )
            click.echo(f"score={match.score:.4f}, {len(match.mapping)} cols [{pairs_str}]")

            if not no_verify:
                click.echo(f"  Verifying rows ... ", nl=False)
                conn = get_connection(cfg.dsn)
                try:
                    ratio = verify_rows(
                        conn, matched, tmpl.columns, db_columns,
                        fuzzy_enabled=fuzzy_enabled,
                        min_containment=cfg.min_containment,
                        verify_sim_threshold=cfg.fuzzy.get("verify_sim_threshold", 0.4),
                    )
                    matched.verified_row_ratio = ratio
                    click.echo(f"{ratio:.1%}")
                finally:
                    conn.close()

            results.append(matched)
        else:
            click.echo("no match")

    click.echo()

    # Sort by score
    results.sort(key=lambda m: m.score, reverse=True)
    results = results[:top]

    output = {
        "template": template,
        "results": [],
        "total_tables": len(tables),
        "generated_at": datetime.now().isoformat(),
    }

    for match in results:
        result = {
            "table": f"{match.schema}.{match.table_name}",
            "score": round(float(match.score), 4),
            "verified_row_ratio": float(match.verified_row_ratio) if match.verified_row_ratio is not None else None,
            "mapping": [
                {
                    "template_col": int(m.template_col_idx),
                    "template_name": m.template_col_name,
                    "db_column": m.db_column,
                    "containment": round(float(m.containment), 4),
                    "exact_containment": round(float(m.exact_containment), 4),
                    "ngram_containment": round(float(m.ngram_containment), 4) if m.ngram_containment is not None else None,
                    "unique": int(m.nd),
                }
                for m in match.mapping
            ],
            "unmatched_template_cols": [int(i) for i in match.unmatched_template_cols],
            "candidates": [
                {
                    "template_col": int(c.template_col_idx),
                    "template_name": c.template_col_name,
                    "db_column": c.db_column,
                    "containment": round(float(c.containment), 4),
                    "exact_containment": round(float(c.exact_containment), 4),
                    "ngram_containment": round(float(c.ngram_containment), 4) if c.ngram_containment is not None else None,
                    "kind": (
                        "fuzzy"
                        if (c.ngram_containment is not None
                            and c.ngram_containment > c.exact_containment)
                        else "exact"
                    ),
                    "unique": int(c.nd),
                }
                for c in getattr(match, "candidates", [])
            ],
        }
        output["results"].append(result)

    # Print report
    if not results:
        click.echo("No matching tables found.")
    else:
        click.echo(f"Top {len(results)} results:")
        click.echo()
        for i, r in enumerate(output["results"], 1):
            click.echo(f"  {i}. {r['table']}")
            click.echo(f"     Score: {r['score']:.4f}")
            if r["verified_row_ratio"] is not None:
                click.echo(f"     Verified rows: {r['verified_row_ratio']:.1%}")
            click.echo(f"     Column mapping:")
            for m in r["mapping"]:
                click.echo(f"       {m['template_name']:20s} -> {m['db_column']:30s}  containment={m['containment']:.1%}  unique={m['unique']:,}")
            if r["unmatched_template_cols"]:
                click.echo(f"     Unmatched: {r['unmatched_template_cols']}")
            if r["candidates"]:
                click.echo(f"     All matches (exact/fuzzy):")
                for c in r["candidates"]:
                    ex = f"{c['exact_containment']:.1%}"
                    ng = f"{c['ngram_containment']:.1%}" if c["ngram_containment"] is not None else "—"
                    click.echo(
                        f"       [{c['kind']:5s}] {c['template_name']:20s} -> {c['db_column']:30s}  "
                        f"exact={ex:>6}  fuzzy={ng:>6}  unique={c['unique']:,}"
                    )
            click.echo()

    if out:
        with open(out, "w") as f:
            json.dump(output, f, indent=2)
        click.echo(f"JSON written to {out}")


def _build_compare_data(conn, template_path, sheet, no_header, tmpl, db_columns, match,
                        cfg, fuzzy_enabled, limit, columns_mode, only_matched,
                        all_template_cols, no_verify):
    """Verify rows + build the comparison data dict for one table.

    Shared by single-table (`compare --table`) and batch (`compare --top`) modes.
    """
    from tablefp.compare import build_comparison
    from tablefp.template import load_raw_columns

    if not no_verify:
        try:
            match.verified_row_ratio = verify_rows(
                conn, match, tmpl.columns, db_columns,
                fuzzy_enabled=fuzzy_enabled,
                min_containment=cfg.min_containment,
                verify_sim_threshold=cfg.fuzzy.get("verify_sim_threshold", 0.4),
            )
        except Exception as e:
            logger.debug(f"verify failed: {e}")

    extra_target_columns = []
    if columns_mode == "all" or all_template_cols:
        matched_names = {tmpl.columns[m.template_col_idx].name for m in match.mapping}
        shown_names = {tc.name for tc in tmpl.columns}
        for rc in load_raw_columns(template_path, sheet_name=sheet,
                                   header=None if not no_header else False):
            if rc.name not in shown_names and rc.name not in matched_names:
                extra_target_columns.append(rc)

    return build_comparison(
        conn, tmpl, db_columns, match,
        limit=limit, fuzzy_enabled=fuzzy_enabled,
        min_containment=cfg.min_containment,
        ngram_size=cfg.fuzzy.get("ngram_size", 3),
        verify_sim_threshold=cfg.fuzzy.get("verify_sim_threshold", 0.4),
        columns_mode=columns_mode, extra_target_columns=extra_target_columns,
        only_matched=only_matched, all_template_columns=all_template_cols,
    )


@cli.command()
@click.argument("template")
@click.option("--config", required=True, help="Path to config.yaml")
@click.option("--table", "table", default=None, help="Target table as schema.table (single-table mode)")
@click.option("--top", "top", type=int, default=None,
              help="Batch mode: generate compare reports for top N matched tables by score")
@click.option("--out", "-o", default="compare.html",
              help="Output HTML file (single) or directory (batch, default compare_reports)")
@click.option("--limit", type=int, default=None, help="Max rows to compare (default: unlimited)")
@click.option("--sheet", help="Sheet name (default: first sheet)")
@click.option("--no-header", is_flag=True, help="Template has no header row")
@click.option("--no-verify", is_flag=True, help="Skip row verification")
@click.option("--columns", "columns_mode", type=click.Choice(["all", "matched"]), default="all",
              help="Show all columns or only matched columns (default: all)")
@click.option("--only-matched", is_flag=True, help="Show only rows with at least one column match")
@click.option("--all-template-cols/--no-all-template-cols", "all_template_cols", default=True,
              help="Show all template columns even without matches (default: on)")
@click.option("--min-distinct", type=int, help="Override min_template_distinct threshold")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
@click.option("--fuzzy/--no-fuzzy", default=None, help="Enable/disable fuzzy matching (default: from config)")
def compare(template: str, config: str, table: Optional[str], top: Optional[int], out: str,
            limit: Optional[int], sheet: str, no_header: bool, no_verify: bool, columns_mode: str,
            only_matched: bool, all_template_cols: bool, min_distinct: Optional[int],
            verbose: bool, fuzzy: Optional[bool]):
    """Side-by-side row comparison of a template against matched table(s).

    Single-table mode: --table SCHEMA.TABLE writes one compare.html.
    Batch mode: --top N matches all indexed tables, sorts by score desc, and
    writes one compare report per top-N table plus an index.html into the output
    directory.
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if (table is None) == (top is None):
        click.echo("ERROR: provide either --table SCHEMA.TABLE or --top N", err=True)
        sys.exit(1)

    cfg = Config.from_yaml(config)
    store = create_store(cfg)
    fuzzy_enabled = cfg.fuzzy.get("enabled", False) if fuzzy is None else fuzzy
    min_tmpl_distinct = cfg.min_template_distinct if min_distinct is None else min_distinct

    conn = get_connection(cfg.dsn)
    try:
        try:
            tmpl = load_template(
                template, conn,
                sheet_name=sheet,
                header=None if not no_header else False,
                min_template_distinct=min_tmpl_distinct,
                fuzzy_enabled=fuzzy_enabled,
                ngram_size=cfg.fuzzy.get("ngram_size", 3),
            )
        except Exception as e:
            click.echo(f"ERROR: Failed to load template: {e}", err=True)
            sys.exit(1)

        if not tmpl.columns:
            click.echo("ERROR: No valid columns in template", err=True)
            sys.exit(1)

        if table is not None:
            _compare_single(template, table, out, tmpl, store, conn, cfg, fuzzy_enabled,
                            limit, sheet, no_header, no_verify, columns_mode,
                            only_matched, all_template_cols)
        else:
            _compare_batch(template, top, out, tmpl, store, conn, cfg, fuzzy_enabled,
                           limit, sheet, no_header, no_verify, columns_mode,
                           only_matched, all_template_cols)
    finally:
        conn.close()


def _compare_single(template, table, out, tmpl, store, conn, cfg, fuzzy_enabled,
                    limit, sheet, no_header, no_verify, columns_mode,
                    only_matched, all_template_cols):
    if "." not in table:
        click.echo("ERROR: --table must be schema.table", err=True)
        sys.exit(1)
    schema, _, table_name = table.partition(".")

    db_columns = [
        c for c in store.list_columns()
        if c.schema == schema and c.table_name == table_name
    ]
    if not db_columns:
        click.echo(f"ERROR: table {table} not found in index", err=True)
        sys.exit(1)

    match = match_table(
        tmpl, db_columns, store,
        min_containment=cfg.min_containment,
        fuzzy_enabled=fuzzy_enabled,
        fuzzy_alpha=cfg.fuzzy.get("alpha", 0.8),
        candidate_min_containment=cfg.candidate_min_containment,
        fuzzy_metric=cfg.fuzzy.get("metric", "jaccard"),
    )
    if match is None:
        click.echo(f"No column match between template and {table}.", err=True)
        sys.exit(1)

    cands = getattr(match, "candidates", None) or match.mapping
    click.echo(f"Column candidates above min_containment={cfg.min_containment} ({len(cands)}):")
    for c in cands:
        ex = f"{c.exact_containment:.1%}"
        ng = f"{c.ngram_containment:.1%}" if c.ngram_containment is not None else "—"
        click.echo(f"  {c.template_col_name} -> {c.db_column}  exact={ex}  fuzzy={ng}")
    click.echo()

    data = _build_compare_data(conn, template, sheet, no_header, tmpl, db_columns, match,
                               cfg, fuzzy_enabled, limit, columns_mode, only_matched,
                               all_template_cols, no_verify)

    click.echo(f"Columns with actual row hits ({len(data['matched_pairs'])}):")
    for p in data["matched_pairs"]:
        hr = p.get("hit_rows", 0)
        tr = p.get("total_rows", 0)
        pct = f"{hr}/{tr} ({hr*100//tr}%)" if tr else "—"
        click.echo(f"  {p['source_col']:30s}  hits={pct}")
    click.echo()

    from tablefp.visualize import generate_comparison_report
    generate_comparison_report(data, out)


def _compare_batch(template, top, out, tmpl, store, conn, cfg, fuzzy_enabled,
                   limit, sheet, no_header, no_verify, columns_mode,
                   only_matched, all_template_cols):
    outdir = out if out != "compare.html" else "compare_reports"
    os.makedirs(outdir, exist_ok=True)

    all_columns = store.list_columns()
    tables: dict = {}
    for col in all_columns:
        key = (col.schema, col.table_name)
        tables.setdefault(key, []).append(col)
    click.echo(f"Tables to match: {len(tables)}")

    matched = []
    for (schema, table_name), db_columns in tables.items():
        click.echo(f"Matching {schema}.{table_name} ... ", nl=False)
        m = match_table(
            tmpl, db_columns, store,
            min_containment=cfg.min_containment,
            fuzzy_enabled=fuzzy_enabled,
            fuzzy_alpha=cfg.fuzzy.get("alpha", 0.8),
            candidate_min_containment=cfg.candidate_min_containment,
            fuzzy_metric=cfg.fuzzy.get("metric", "jaccard"),
        )
        if m is None or not m.mapping:
            click.echo("no match")
            continue
        click.echo(f"score={m.score:.4f}")
        matched.append((m, db_columns))

    matched.sort(key=lambda x: x[0].score, reverse=True)
    matched = matched[:top]
    if not matched:
        click.echo("No matching tables found.")
        return

    click.echo(f"Generating compare reports for top {len(matched)} tables ...")

    from tablefp.visualize import generate_comparison_report, generate_compare_index

    entries = []
    for i, (m, db_columns) in enumerate(matched, 1):
        data = _build_compare_data(conn, template, sheet, no_header, tmpl, db_columns, m,
                                   cfg, fuzzy_enabled, limit, columns_mode, only_matched,
                                   all_template_cols, no_verify)
        fname = f"{m.schema}.{m.table_name}.html"
        generate_comparison_report(data, os.path.join(outdir, fname))
        entries.append({
            "file": fname,
            "table": f"{m.schema}.{m.table_name}",
            "score": float(m.score),
            "verified": m.verified_row_ratio,
            "n_matched": sum(1 for r in data["rows"] if r.get("matched")),
            "n_rows": len(data["rows"]),
        })
        click.echo(f"  {i:02d}. {m.schema}.{m.table_name}  score={m.score:.4f}  -> {fname}")

    idx_path = os.path.join(outdir, "index.html")
    generate_compare_index(entries, idx_path, template_name=template)
    click.echo()
    click.echo(f"Index: {idx_path}")


@cli.command()
@click.argument("files", nargs=-1, required=True)
@click.option("--out", "-o", default="report.html", help="Output HTML file")
def report(files: tuple, out: str):
    """Generate HTML report from search result JSON files.

    \b
    Examples:
      tablefp report result.json
      tablefp report result1.json result2.json -o merged.html
      tablefp report results/*.json
    """
    from tablefp.visualize import generate_report
    generate_report(list(files), out)


if __name__ == "__main__":
    cli()