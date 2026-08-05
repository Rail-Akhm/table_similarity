# CLAUDE.md — tablefp

## Обзор

**tablefp** — поиск таблиц Greenplum, похожих на xlsx-шаблон. Две фазы:
- **Phase 1 (exact)** — containment через 64-битные MD5-хэши
- **Phase 2 (fuzzy)** — n-граммное (триграммное) сравнение текстовых колонок

## Структура файлов

```
table_similarity/
├── config.example.yaml       # Шаблон конфига (без креденшелов)
├── config/                   # gitignored — реальный config.yaml
├── .gitignore                # config/, config.yaml, __pycache__, fp_index/
├── run_tablefp.py            # Точка входа без pip install
├── tablefp/
│   ├── __init__.py           # version = "0.1.0"
│   ├── __main__.py           # python -m tablefp
│   ├── cli.py                # Click CLI: index, search, compare, report, run
│   ├── config.py             # Config dataclass + from_yaml() + parse_dsn()
│   ├── catalog.py            # Обход information_schema + fnmatch-паттерны
│   ├── db.py                 # psycopg2: get_connection, get_cursor, iter_cursor
│   ├── norm.py               # SQL-нормализация и хэширование (НЕ трогать!)
│   ├── hashing.py            # Python: h64, ngrams, trigram_sim, row_similarity, add_ngram_hashes
│   ├── store.py              # ArtifactStore (sqlite+.npy) + DBArtifactStore (GP)
│   ├── indexer.py            # Построение отпечатков (многопоточно, ThreadPoolExecutor)
│   ├── template.py           # Загрузка xlsx + хэширование через БД (unnest)
│   ├── matcher.py            # 3-stage matching: prefilter → containment → Hungarian
│   ├── verify.py             # Stage 3: построчная верификация
│   ├── compare.py            # Построение данных для сравнения (source-driven)
│   ├── visualize.py          # HTML-отчёты: report.html, compare.html (gzip+base64), index.html
│   └── tests/                # 72 юнит-теста + 9 интеграционных (нужна БД)
```

## Конфигурация (`config/config.yaml`)

```yaml
dsn: "postgresql://user:pass@host:5432/db"  # обязательно
tables: ["ods_suoi.*"]                        # fnmatch-паттерны таблиц
exclude_columns: []                           # schema.table.column
exclude_column_patterns: ["s__*"]             # fnmatch на имя колонки
dtype_groups: ["text"]                        # опциональный фильтр (text/num/date/ts/bool/uuid)

store_type: "local"             # local (sqlite+.npy) или db (Greenplum tablefp_columns)
index_dir: "./fp_index"         # для local
max_workers: 4                  # соединений с БД (на сервере лимит 5)

skip_text_avg_len: 500          # пропускать text-колонки с avg_len > N
min_containment: 0.3            # порог containment для pairing/scoring
candidate_min_containment: 0.3  # порог для candidates (не влияет на scoring)
min_template_distinct: 5        # мин. уникальных значений в колонке шаблона

fuzzy:
  enabled: false                # Phase 2: n-граммное сравнение
  ngram_size: 3                 # размер n-грамм (как pg_trgm)
  max_nd: 2000000               # макс. distinct values для n-грамм
  columns: []                   # whitelist (fnmatch)
  alpha: 0.8                    # S = max(exact, alpha × ngram)
  metric: jaccard               # jaccard или coverage_weighted
  verify_sim_threshold: 0.4     # порог trigram_sim для строковой верификации
```

Config загружается через `Config.from_yaml(path)`. Обязательное поле: `dsn`.

## CLI-команды

```bash
# Индексация
tablefp index --config config/config.yaml [--force] [--tables ...] [-v]

# Поиск
tablefp search template.xlsx --config config/config.yaml [--top 5] [--fuzzy] [--out results.json]

# Сравнение (одна таблица)
tablefp compare template.xlsx --config config/config.yaml --table schema.table [--fuzzy] [-o compare.html]

# Сравнение (топ-N таблиц)
tablefp compare template.xlsx --config config/config.yaml --top 10 [--fuzzy] -o compare_reports/

# Полный цикл (search + report + compare) одной командой
tablefp run template.xlsx --config config/config.yaml --top 10 [--fuzzy] -o run_reports/

# HTML-отчёт из JSON
tablefp report results.json -o report.html
```

Без установки: `python run_tablefp.py <command> ...`

## Ключевые модули

### `norm.py` — SQL-нормализация и хэши

**Критическое правило: вся нормализация и хэширование — только через SQL.**
Никогда не нормализуй значения в Python.

Шесть dtype-групп: `text`, `num`, `date`, `ts`, `bool`, `uuid`.

| Группа | Нормализация | Хэш |
|--------|-------------|-----|
| text/uuid | `lower(btrim(regexp_replace(x, '\s+', ' ', 'g')))` | `('x' \|\| substr(md5(norm), 1, 16))::bit(64)::bigint` |
| num | round до 6 dec, strip trailing zeros | md5 → int64 |
| date | `to_char(x, 'YYYY-MM-DD')` | md5 → int64 |
| ts | `to_char(x, 'YYYY-MM-DD"T"HH24:MI:SS')` | md5 → int64 |
| bool | `CASE WHEN x THEN 'true' ELSE 'false' END` | md5 → int64 |

Функции: `get_norm_expr(dtype_group, column)`, `get_h64_expr(dtype_group, column)`, `build_h64_select(dtype_group, column)`.

Константы: `NUM_TEST_VECTORS`, `ABC_HASH_EXPECTED = -8070080442485551184`.

### `hashing.py` — N-граммы и сходство строк

N-граммное хэширование в Python, но на значениях, уже нормализованных SQL.

| Функция | Назначение |
|---------|-----------|
| `h64(s)` | md5 первых 8 байт → signed int64 (идентично SQL) |
| `ngrams(s, n=3)` | Множество n-грамм с пробельными padding |
| `trigram_sim(a, b)` | Jaccard-сходство: `|A∩B| / |A∪B|` |
| `row_similarity(tmpl, db)` | Одностороннее containment: `|A∩B| / |A|` |
| `add_ngram_hashes(values, target_set, n)` | **Потоковая** версия: добавляет h64 каждой n-граммы в переданный `set` |
| `build_ngram_hashes(values, n)` | Полная сборка: вызывает `add_ngram_hashes` → sorted int64 array |
| `shared_trigram_spans(a, b, n)` | Диапазоны символов в `a`, покрытые общими с `b` триграммами |

**Важно:** `add_ngram_hashes` принимает генератор + мутабельный set — не создаёт промежуточных списков. Используется в `indexer.py` для экономии памяти.

### `indexer.py` — Индексация колонок

**`index_column(conn, store, column, skip_text_avg_len, force, fuzzy_config)`**

Для каждой колонки:
1. **Stats-запрос** — `COUNT`, `COUNT DISTINCT norm`, плюс min/max/percentile_disc (num/date/ts) или avg_len (text)
2. **Фильтр text** — пропуск если `avg_len > skip_text_avg_len`
3. **Стриминг хэшей** — `SELECT DISTINCT h64` → fetchmany(50000) → `np.fromiter` → `np.concatenate` → `np.sort` → `.npy`
4. **N-граммы** (fuzzy + text): `SELECT DISTINCT norm` → fetchmany(50000) → `add_ngram_hashes(generator, set)` → `np.fromiter` → `np.sort` → `.ngrams.npy`
5. **ColumnRecord** → `store.upsert_column()`

**Память:** хэши собираются через `np.fromiter` чанками (not Python int list), n-граммы через генератор в мутабельный set. Для колонки с 5M уникальных значений пик памяти ~200 MB (основной массив int64).

**`index_tables(...)`** — оркестратор:
- `crawl_columns()` → список колонок
- `ThreadPoolExecutor(max_workers)` — каждый worker своё соединение
- `tqdm` progress bar: desc="Indexing", postfix=текущая колонка

### `matcher.py` — Сопоставление шаблона с таблицей

Три этапа:

**Stage 0 — `stage0_prefilter(template, db_columns)`:**
- Совместимость dtype (`is_compatible`): num↔num, text↔text, date↔date, ts↔ts, bool↔bool, uuid↔text, text↔num/date (fallback)
- Пересечение numeric-диапазонов
- Дедупликация (каждая DB-колонка один раз)

**Stage 1 — `stage1_containment(template, compatible, store, ...)`:**
- Возвращает три матрицы `(S, S_exact, S_ngram)` формы `(n_tmpl, n_db)`
- Загружает `db_hashes` и `db_ngrams` по одной колонке за раз, **без кэша** — пик памяти: одна колонка, не вся таблица
- `S[i,j] = max(exact, α × ngram)` для text-text, иначе exact
- `S_exact[i,j]` — чистое exact containment
- `S_ngram[i,j]` — NaN где не вычислялось

**Stage 2 — `stage2_assignment(S, compatible, min_containment)`:**
- Вес селективности: `W[j] = log2(nd_j + 1)`
- Hungarian algorithm (`scipy.optimize.linear_sum_assignment(-S × W)`)
- Фильтр: S ≥ min_containment
- Score: `Σ(S·W) / Σ(W) × coverage`, где coverage = matched/total

**`match_table()`** объединяет этапы, возвращает `TableMatch`:
- `mapping` — 1:1 назначенные пары (для scoring)
- `candidates` — все пары где exact ИЛИ ngram ≥ `candidate_min_containment` (для отчёта)
- Читает exact/ngram из матриц (не перезагружает .npy)

**Метрики fuzzy:**
- `jaccard` — `|A∩B| / |A∪B|`, анти-«губка»
- `coverage_weighted` — `|A∩B|² / (|A|·|A∪B|)`, приоритет high-hit колонкам

### `verify.py` — Построчная верификация

**`verify_rows(conn, match, template_columns, db_columns, ...)`:**
- Выбор anchor через `select_anchor()`: предпочитает exact с nd > 100
- **Быстрый путь:** `JOIN unnest(anchor_values) ON norm = value`
- **Fuzzy fallback:** если anchor не дал строк → сканирует подмножество, `trigram_sim ≥ threshold`
- Строка считается совпавшей если ≥80% колонок совпали (hash equality или `row_similarity`)
- Возвращает ratio (0.0–1.0)

### `compare.py` — Данные для сравнения

**`build_comparison(conn, template, db_columns, match, ...)`:**
- **Source-driven:** строки БД — источник, строки шаблона подбираются через anchor
- Использует ВСЕ candidates (не только 1:1 mapping)
- Первичный anchor (exact) + вторичный fuzzy anchor для fallback
- `_classify_pair()`: exact (норм. равны), fuzzy (`row_similarity ≥ threshold`), none
- `shared_trigram_spans()` — подсветка общих триграмм
- Фильтрация «мёртвых» колонок (ноль строк с реальными совпадениями)
- Формат результата: `{"m": bool, "c": [tgt_cells..., src_cells...]}` — target перед source

**Параметр `only_hit_columns`** (по умолчанию True): скрывает колонки без единого cell-совпадения.

### `visualize.py` — HTML-отчёты

Все отчёты самодостаточны (открываются из `file://`).

**report.html** — результаты поиска:
- Прямой рендеринг (мало строк), сортировка, фильтр
- Score bar, mapping (1:1), candidates (все совпадения exact/fuzzy)

**compare.html** — построчное сравнение:
- `_stream_pack()` — потоковая gzip+base64 запись, не держит весь blob в памяти
- Шаблон разделён (`_COMPARE_TEMPLATE_HEAD` / `_COMPARE_TEMPLATE_TAIL`) для вставки blob между
- Виртуальный скроллинг (ROW_H=30px), resizable колонки (localStorage)
- Порядок колонок: target (НСИ) слева, divider, source (БД) справа

**index.html** — индекс для `compare --top N` / `run`.

### `template.py` — Загрузка шаблона

**`load_template(path, conn, ...)`:**
- openpyxl: `read_only=True, data_only=True`
- auto-detect header (первая строка — нечисловые строки)
- `canonicalize_cell()` → `infer_dtype_group()` (≥90% чисел → num, ≥90% дат → date)
- Хэширование через БД: `SELECT norm, h64 FROM unnest(cols, vals)` — единый запрос на колонку
- N-граммы: `build_ngram_hashes(norm_values)` на SQL-нормализованных значениях

**`load_raw_columns()`** — для `--columns all`: загружает все колонки без хэширования.

## Поток данных

```
index:
  Config → crawl_columns (information_schema) → ColumnInfo[]
  → ThreadPoolExecutor → index_column() для каждой:
    1. SQL stats (n, nd, min/max/quantiles или avg_len)
    2. SQL SELECT DISTINCT h64 → np.fromiter чанками → np.concatenate → np.sort → .npy
    3. Если fuzzy+text: SELECT DISTINCT norm → add_ngram_hashes(генератор, set) → .ngrams.npy
    4. ColumnRecord → catalog.db

search:
  Config → load_template(xlsx):
    1. openpyxl → rows → canonicalize_cell → infer_dtype_group
    2. SQL: unnest(vals) → (norm_v, h64) для каждой строки
    3. fuzzy+text: build_ngram_hashes(norm_values)
  → Для каждой таблицы: match_table() → stage0 → stage1 → stage2 → TableMatch
  → verify_rows() (опционально)

compare:
  match_table() + build_comparison():
    1. Все candidates → col_info → select_anchor
    2. SQL: SELECT raw + norm для source/target колонок
    3. Выравнивание через anchor (exact) + fuzzy fallback
    4. _classify_pair → spans
  → HTML (gzip+base64 blob)

run:
  match_all_tables() → verify → search.json → report.html → compare_reports/
```

## Тестирование

```bash
pytest tablefp/tests/                           # 72 unit-теста (БД не нужна)
pytest tablefp/tests/test_norm.py -m integration  # 9 интеграционных (POSTGRES_DSN)
```

Маркер `integration` зарегистрирован в `conftest.py`. Фикстура `db_dsn` читает `POSTGRES_DSN`.

## Ключевые правила

1. **Нормализация только в SQL.** `norm.py` — единственный источник нормализации и хэширования.
2. **Стабильность хэшей.** `h64('abc') ≡ -8070080442485551184`. Менять алгоритм нельзя без переиндексации.
3. **Конфиденциальность.** `config/` и `config.yaml` в `.gitignore`. `config.example.yaml` — шаблон.
4. **mmap-загрузка.** `.npy` загружаются через `mmap_mode="r"` — эффективно для больших массивов.
5. **PG 9.4 совместимость.** `md5()`, `substr()`, `::bit(64)::bigint` — работают в Greenplum 6.24.
6. **Source-driven compare.** Строки БД — источник, шаблон подбирается к ним.
7. **Scoring.** `Σ(S·W)/Σ(W) × coverage` — приоритет таблицам с бо́льшим покрытием колонок шаблона.
8. **Compare cells.** Target (НСИ) слева, source (БД) справа. Порядок в `c`: сначала target-ячейки, потом source-ячейки.
9. **`_stream_pack`.** Потоковая запись gzip+base64 для compare.html — не держит весь blob в RAM.
10. **`only_hit_columns`.** По умолчанию True — скрывает колонки без реальных cell-совпадений.

## Зависимости

- Python 3.10+
- Greenplum 6.24 / PostgreSQL 9.4+
- `psycopg2-binary`, `numpy`, `scipy`, `openpyxl`, `pyyaml`, `click`, `tqdm`
