# tablefp — Fuzzy search of DB tables by xlsx template

Given a small template table (xlsx), find which Greenplum tables contain similar data.

Supports **exact containment matching** (Phase 1) and **n-gram fuzzy matching**
(Phase 2) to tolerate typos and small edits in text columns.

## Examples

Пошаговые примеры на русском (сканирование → поиск → сравнение → отчёты)
см. в [EXAMPLES.md](EXAMPLES.md).

## Quick start

```bash
# Without pip install
python run_tablefp.py index --config config.yaml
python run_tablefp.py search fields.xlsx --config config.yaml --top 5

# Or install as package
pip install -e .
tablefp index --config config.yaml
tablefp search fields.xlsx --config config.yaml --top 5
```

## Commands

### `index` — Build column fingerprints

```bash
tablefp index --config config.yaml          # index all configured tables
tablefp index --config config.yaml --force  # re-index already indexed columns
tablefp index --config config.yaml -v       # verbose (show every column)
```

For each column: computes stats (n, unique, min/max, quantiles, avg_len),
streams sorted int64 hashes → `.npy` (or DB BYTEA). When fuzzy is enabled,
also builds n-gram hash union for eligible text columns → `.ngrams.npy`.

### `search` — Find matching tables

```bash
tablefp search fields.xlsx --config config.yaml --top 5
tablefp search fields.xlsx --config config.yaml --top 5 --out results.json
tablefp search fields.xlsx --config config.yaml --no-verify  # skip row check
tablefp search fields.xlsx --config config.yaml --fuzzy       # force fuzzy on
```

| Option | Description |
|---|---|
| `--top N` | Show top N results (default: 5) |
| `--no-verify` | Skip stage-3 row verification |
| `--no-header` | Template has no header row |
| `--sheet NAME` | Use specific sheet (default: first) |
| `--out FILE` | Write results to JSON |
| `--fuzzy` / `--no-fuzzy` | Enable/disable fuzzy matching (default: from config) |
| `-v`, `--verbose` | Debug-level logging |

Besides the 1:1 assigned column mapping (used for scoring), each result also
lists **all matches** — every DB column whose exact **or** fuzzy containment is
above `candidate_min_containment` for a given template column. This exposes
cases the Hungarian assignment hides (e.g. a template column that legitimately
matches several DB columns). In the JSON and HTML report each candidate is
tagged `exact` (green) or `fuzzy` (orange) and shows both `exact` and `fuzzy`
containment percentages.

`candidate_min_containment` (default = `min_containment`) sets the bar for this
list only, without affecting the score. To surface weak **fuzzy** matches — e.g.
a deposit name that appears only as a *substring* of a long free-text column —
set `fuzzy.enabled: true` and lower `candidate_min_containment` (e.g. `0.09`).
The assigned/scored match is unchanged; the weak match just becomes visible as
an extra `fuzzy` candidate.

**Fuzzy metric** (`fuzzy.metric`): how column-level trigram similarity is scored.
- `jaccard` (default): `|A∩B| / |A∪B|`. Symmetric; a huge free-text column ranks
  low even if it shares many trigrams (anti-"sponge").
- `coverage_weighted`: `|A∩B|² / (|A|·|A∪B|)`. Rewards the *number* of template
  trigrams hit, so a column with many hits (e.g. a long `division` column that
  embeds deposit names as substrings) ranks **above** incidental trigram noise
  like unrelated accounting terms — while a genuine same-size match still wins.
  Choose this if you want high-hit columns prioritized over small-but-noisy ones.

### `report` — Generate HTML report

```bash
tablefp report results.json -o report.html
tablefp report run1.json run2.json -o merged.html
```

Self-contained HTML file with sortable columns, score bars, column mapping pairs,
search/filter. No dependencies.

### `compare` — Side-by-side row comparison

```bash
tablefp compare fields.xlsx --config config.yaml --table dwh.orders -o compare.html
tablefp compare fields.xlsx --config config.yaml --table dwh.orders --columns matched
tablefp compare fields.xlsx --config config.yaml --table dwh.orders --limit 1000
tablefp compare fields.xlsx --config config.yaml --table dwh.orders --no-verify
```

Batch mode: `--top N` matches all indexed tables, sorts by score desc, and
writes one compare report per top-N table plus an `index.html` into the output
directory:

```bash
tablefp compare fields.xlsx --config config.yaml --top 10 -o compare_reports/
```

Produces a self-contained, source-driven HTML: the **source (DB) table rows are
shown on the left**, and the **matching template row is shown on the right**,
aligned via the best-matched *anchor* column. Matched cells are highlighted on
both sides:

- **Exact** matches (normalized values equal) are highlighted green.
- **Fuzzy** matches (text columns, when `--fuzzy`) highlight the shared trigram
  character spans in orange.
- Every source row is shown (use `--limit N` to cap); rows with no
  template match are dimmed and tagged `no match`.

**All** matched columns are shown, not just the single assigned pair — every DB
column whose exact or fuzzy containment is above `min_containment` is tinted and
highlighted (e.g. a template column that matches both `mestorozhdenie` and
`mestorozhdenie_kp` shows both). Rows are aligned on the single best *anchor*.

`--columns` controls width:

- `all` (default) — show every column of both tables; matched columns are tinted
  in the header, unmatched columns are shown for context without highlighting.
- `matched` — show only the matched columns (all of them, across candidates).

Displayed text is the raw value as stored; matching/highlighting is computed on
the normalized form. Requires live DB access (raw values are not indexed).

## Configuration (`config.yaml`)

```yaml
# ── Connection ──
dsn: "postgresql://user:password@host:5432/dbname"

# ── Tables ──
# Supports fnmatch globs (*, ?) in schema and table name
tables:
  - "dwh.*"              # all tables in dwh schema
  - "stage.orders"       # exact table
  - "dp_rid_*.*"         # all tables in schemas matching dp_rid_*
  - "dp_rid_*.fact_*"    # tables matching fact_* in dp_rid_* schemas

# ── Column filters ──
exclude_columns: []                       # exact: "schema.table.column"
exclude_column_patterns: ["s_*", "tmp_*"] # fnmatch glob on column name
# dtype_groups: ["text"]                  # only index these dtype groups

# ── Storage ──
store_type: "local"            # "local" (sqlite+.npy) or "db" (Greenplum table)
index_dir: "./fp_index"        # directory for local store
# storage_dsn: "postgresql://..."  # DB store connection (defaults to dsn)

# ── Performance ──
max_workers: 4                 # concurrent DB connections during indexing
skip_text_avg_len: 500         # skip text columns with avg length > this

# ── Matching ──
min_containment: 0.3           # minimum containment for column pairing
min_template_distinct: 5       # skip template columns with < N distinct values

# ── Fuzzy matching ──
# Tolerates typos/small edits in text columns (e.g. 'иванов' vs 'ивонов').
# When disabled, behaviour is byte-identical to Phase 1.
fuzzy:
  enabled: false
  ngram_size: 3                # n-gram size (like pg_trgm)
  max_nd: 2000000              # max distinct values for n-gram indexing
  columns: []                  # optional column whitelist (fnmatch globs)
  alpha: 0.8                   # S = max(exact, alpha * ngram)
  verify_sim_threshold: 0.4    # trigram_sim threshold for fuzzy row verification
```

## Architecture

### Phase 1 — exact containment

1. **Indexing** — per column: stats (n, unique, min/max, quantiles, avg_len)
   + sorted int64 hashes → `.npy` or DB BYTEA.
2. **Template loading** — read xlsx, canonicalize values, hash through DB using
   the same SQL expressions as indexing. Returns both normalized values and
   hashes per row.
3. **Matching** — 4 stages:
   - **Stage 0**: Prefilter by dtype compatibility and numeric range overlap.
   - **Stage 1**: Containment matrix — `np.isin` of template hashes in DB column.
   - **Stage 2**: Hungarian assignment weighted by selectivity (`log2(nd)`).
   - **Stage 3**: Row verification — anchor column join, ≥80% column match.

### Phase 2 — n-gram fuzzy (opt-in)

1. **Indexing** — for eligible text columns, stream *normalized* values,
   build n-gram hash union in Python → `.ngrams.npy`.
2. **Template** — compute n-gram hashes for text columns.
3. **Matching** — `S[i,j] = max(exact, alpha * ngram)` for text-text pairs.
   Exact pairs unaffected.
4. **Verification** — fuzzy-matched columns use `trigram_sim` instead of
   hash equality. Exact-matched anchor preferred.

### Scoring formula

```
W[j]  = log2(nd_j + 1)          # selectivity weight
pairs = hungarian(max S[i,j] × W[j])   # optimal assignment
score = Σ(S[p] × W[p]) / Σ(W[p])       # weighted average
score *= matched_cols / total_template_cols  # coverage penalty
```

Normalization and hashing always run in SQL. N-gram hashing runs in Python
but operates on *SQL-normalized* values only.

## Normalization & hashing

| Type | SQL |
|---|---|
| Text / UUID | `lower(btrim(regexp_replace(x, '\s+', ' ', 'g')))` |
| Numeric | Round to 6 decimals, strip trailing zeros |
| Date / TS | `to_char(x, 'YYYY-MM-DD')` / `'YYYY-MM-DD"T"HH24:MI:SS'` |
| Boolean | `CASE WHEN x THEN 'true' ELSE 'false' END` |
| Hash (SQL) | `('x' \|\| substr(md5(norm), 1, 16))::bit(64)::bigint` |
| Hash (Python) | `int.from_bytes(md5(s).digest()[:8], 'big', signed=True)` |

## Testing

```bash
# All unit tests (38, no DB needed)
pytest tablefp/tests/

# Integration tests (requires DB)
export POSTGRES_DSN="postgresql://..."
pytest tablefp/tests/test_norm.py -m integration
```

## Requirements

- Python 3.10+
- Greenplum 6.24 / PostgreSQL 9.4+
- Dependencies: `psycopg2-binary`, `numpy`, `scipy`, `openpyxl`, `pyyaml`, `click`
