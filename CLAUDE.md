# CLAUDE.md — tablefp

## Обзор проекта

**tablefp** — инструмент для нечёткого поиска таблиц Greenplum по xlsx-шаблону.
Дано: небольшой шаблон (xlsx с 10–100 строками). Задача: найти, в каких таблицах
Greenplum содержатся похожие данные, и показать построчное сравнение.

Два режима сравнения:
- **Phase 1 (exact)** — точное containment-сопоставление через 64-битные хэши MD5.
- **Phase 2 (fuzzy)** — n-граммное (триграммное) нечёткое сопоставление текстовых
  колонок, устойчивое к опечаткам и мелким правкам (например, «иванов» vs «ивонов»).

## Архитектура

```
config.yaml  →  Config  →  indexer  →  SQL stats + хэши  →  .npy / DB BYTEA
                                  →  n-граммные хэши     →  .ngrams.npy

template.xlsx  →  load_template()  →  TemplateColumn[]  →  хэши через БД (unnest)
                                   →  n-граммные хэши (Python)

match_table()  →  Stage 0: prefilter (dtype + range)
              →  Stage 1: containment matrix (np.isin + ngram jaccard/coverage_weighted)
              →  Stage 2: Hungarian assignment + scoring

verify_rows()  →  Stage 3: anchor column join, ≥80% column match threshold

build_comparison()  →  source-driven: DB rows (left) vs template row (right)

visualize.py  →  report.html (search results), compare.html (virtual scroll), index.html (batch)
```

### Поток данных (end-to-end)

```
1. index
   Config → crawl_columns (information_schema) → expand_table_patterns (fnmatch)
   → column_info[] → index_column() для каждой колонки параллельно (ThreadPoolExecutor):
     a. SQL: count(*), count(DISTINCT norm), min/max/quantiles (num) или avg_len (text)
     b. SQL: SELECT DISTINCT h64 → sorted int64[] → .npy (mmap)
     c. Если fuzzy + text + nd ≤ max_nd: SELECT DISTINCT norm → ngrams → .ngrams.npy
   → ColumnRecord → catalog.db (SQLite) или tablefp_columns (Greenplum)

2. search
   Config → load_template(xlsx):
     a. openpyxl (read_only, data_only) → rows
     b. auto-detect header (первая строка — нечисловые строки)
     c. canonicalize_cell (Python, минимально: strip, bool→'true'/'false', float→6 dec)
     d. infer_dtype_group: ≥90% чисел → 'num', ≥90% дат → 'date', иначе 'text'
     e. SQL: SELECT h64 FROM unnest(vals) — хэширование через БД (критично!)
     f. Если fuzzy + text: build_ngram_hashes() в Python на нормализованных значениях
   → Template + TemplateColumn[] (distinct_hashes, row_hashes, row_norm_v, ngram_hashes, min/max)

   Для каждой таблицы в store.list_columns():
     match_table():
       Stage 0: is_compatible(dtype) + numeric range overlap
       Stage 1: S[i,j] = max(exact, α·ngram) для text-text; exact иначе
       Stage 2: Hungarian(S × log2(nd+1)), coverage penalty = matched/total
       Candidates: все пары где exact ИЛИ ngram ≥ candidate_min_containment
     → TableMatch (score, mapping, candidates)

     verify_rows():
       - Выбирает anchor (предпочитает exact, nd > 100)
       - Быстрый путь: JOIN unnest(anchor_values) — точное совпадение anchor
       - Медленный путь (fuzzy fallback): сканирует первые N строк, trigram_sim
       - Для каждой строки шаблона: hash equality (exact) или row_similarity (fuzzy)
       - ≥80% колонок совпало → строка verified
     → verified_row_ratio

3. compare
   match_table() + build_comparison():
     - Source-driven: строки БД слева, совпавшая строка шаблона справа
     - Выравнивание через anchor (exact) + fuzzy fallback anchor
     - _classify_pair: exact (нормализованные равны), fuzzy (row_similarity ≥ threshold), none
     - shared_trigram_spans: диапазоны символов для подсветки общих триграмм
     - Фильтрация «мёртвых» колонок (0 строк с реальными совпадениями)
   → сравнение в HTML (gzip+base64 blob, virtual scroll)

4. report
   generate_report(): читает search JSON(ы) → _render() → report.html
   Самостоятельный HTML: сортировка, фильтр, полосы оценок, legend.
```

## Структура файлов

```
table_similarity/
├── config.example.yaml       # Шаблон конфигурации (без credentials)
├── config/                   # gitignored — config.yaml с реальными DSN
├── run_tablefp.py            # Точка входа без pip install
├── tablefp/
│   ├── __init__.py           # version = "0.1.0"
│   ├── __main__.py           # python -m tablefp
│   ├── cli.py                # Click CLI: index, search, compare, report
│   ├── config.py             # Config dataclass + from_yaml + parse_dsn
│   ├── catalog.py            # information_schema crawler + fnmatch expander
│   ├── db.py                 # psycopg2 connection helpers
│   ├── norm.py               # ⚠️ КРИТИЧНО: SQL-нормализация и хэширование
│   ├── hashing.py            # Python: h64, ngrams, trigram_sim, row_similarity, spans
│   ├── store.py              # ArtifactStore (sqlite+.npy) + DBArtifactStore (GP)
│   ├── indexer.py            # Построение отпечатков колонок (многопоточно)
│   ├── template.py           # Загрузка xlsx + хэширование через БД
│   ├── matcher.py            # 4-stage matching + Hungarian assignment
│   ├── verify.py             # Stage 3: построчная верификация
│   ├── compare.py            # Построение данных для сравнения (source-driven)
│   ├── visualize.py          # HTML-отчёты: report.html, compare.html, index.html
│   └── tests/
│       ├── conftest.py       # markers: integration; fixtures: db_dsn, skip_if_no_db
│       ├── test_norm.py      # SQL norm/hash integration tests (POSTGRES_DSN)
│       ├── test_hashing.py   # h64, ngrams, trigram_sim, row_similarity unit tests
│       ├── test_matcher.py   # compatibility, containment, assignment, candidates
│       ├── test_template.py  # canonicalize_cell, infer_dtype_group
│       ├── test_compare.py   # build_comparison, classify_pair, spans, fuzzy anchor
│       └── test_visualize.py # _render, generate_compare_index, generate_report
└── fp_index/                 # gitignored — каталог локального индекса
```

## Ключевые модули

### `norm.py` — Нормализация и хэширование (SQL)

**Правило №1: вся нормализация и хэширование должны проходить через SQL.**
Никогда не нормализуй и не хэшируй значения в Python.

Шесть dtype-групп: `text`, `num`, `date`, `ts`, `bool`, `uuid`.

| Группа | Нормализация | Хэш |
|--------|-------------|-----|
| text | `lower(btrim(regexp_replace(x, '\s+', ' ', 'g')))` | md5 первых 16 hex → int64 |
| uuid | как text | как text |
| num | round до 6 dec, strip trailing zeros | md5 → int64 |
| date | `to_char(x, 'YYYY-MM-DD')` | md5 → int64 |
| ts | `to_char(x, 'YYYY-MM-DD"T"HH24:MI:SS')` | md5 → int64 |
| bool | `CASE WHEN x THEN 'true' ELSE 'false' END` | md5 → int64 |

Функции:
- `get_norm_expr(dtype_group, column)` → SQL expression
- `get_h64_expr(dtype_group, column)` → `('x' || substr(md5(norm), 1, 16))::bit(64)::bigint`
- `build_h64_select(dtype_group, column)` → SELECT expression

Константы:
- `NUM_TEST_VECTORS` — тестовые векторы для norm_num
- `ABC_HASH_EXPECTED = -8070080442485551184` — ожидаемый хэш строки 'abc'

### `hashing.py` — N-граммное хэширование (Python)

**Важно:** хэширование n-грамм происходит в Python, но на значениях,
*уже нормализованных SQL*. Это единственное исключение из правила
«только SQL».

Функции:
- `h64(s)` — md5 первых 8 байт → signed int64 (идентично SQL `::bigint`)
- `ngrams(s, n=3)` — множество n-грамм с пробельными padding (`" abc "`)
- `trigram_sim(a, b)` — Jaccard-сходство триграмм: `|A∩B| / |A∪B|`
- `row_similarity(tmpl, db)` — одностороннее containment: `|A∩B| / |A|`
  (для короткого шаблона внутри длинной строки БД)
- `build_ngram_hashes(values, n)` — объединение всех n-грамм → sorted int64[]
- `shared_trigram_spans(a, b, n)` — диапазоны символов в `a`, покрытые
  общими с `b` триграммами (для подсветки в HTML)

### `store.py` — Хранилище артефактов

Два бэкенда, выбираются через `create_store(config)`:

**ArtifactStore (local)** — по умолчанию, быстрее:
- SQLite `catalog.db` с метаданными колонок (таблица `columns`)
- `.npy` файлы с отсортированными int64 хэшами (mmap-загрузка)
- `.ngrams.npy` для n-граммных хэшей
- Структура: `fp_index/{schema}/{table}/{column}.npy`
- Миграция схемы: добавляет `ngrams_path` если отсутствует

**DBArtifactStore (db)** — Greenplum таблица `tablefp_columns`:
- Хэши хранятся как BYTEA (npy в бинарном виде)
- Для многопользовательского доступа
- `npy_path` в формате `db://schema.table.column`

`ColumnRecord` — датакласс: schema, table, column, dtype_group, n, nd,
min_val, max_val, quantiles, avg_len, npy_path, ngrams_path, indexed_at.

### `matcher.py` — Сопоставление

**Этапы:**

**Stage 0 (prefilter):**
- `is_compatible(tmpl_group, db_group)` — матрица совместимости типов:
  num↔num, date↔date, ts↔ts, text↔text, bool↔bool, uuid↔text,
  text↔num (fallback), num↔text (fallback)
- Проверка пересечения диапазонов для numeric колонок
- Дедупликация DB колонок (каждая один раз, даже если совместима с несколькими template)

**Stage 1 (containment matrix):**
- `S[i,j]` = доля хэшей template-колонки i, найденных в DB-колонке j
- Использует `np.isin(tmpl_hashes, db_hashes, assume_unique=True).mean()`
- При fuzzy + text: `S[i,j] = max(exact, α × ngram_similarity)`
- N-gram similarity: jaccard или coverage_weighted

**Stage 2 (assignment):**
- Вес селективности: `W[j] = log2(nd_j + 1)`
- Hungarian algorithm (`scipy.optimize.linear_sum_assignment(-S × W)`)
- Фильтр: только пары с S ≥ min_containment
- Score: `Σ(S[p] × W[p]) / Σ(W[p]) × (matched_cols / total_template_cols)`

**Candidates:**
- ВСЕ пары (template col, db col) где exact ИЛИ ngram ≥ candidate_min_containment
- Сортируются по template col index, затем по containment desc
- Не влияют на scoring, только для отчёта

**Метрики fuzzy:**
- `jaccard` (по умолчанию): `|A∩B| / |A∪B|` — симметричная, анти-«губка»
- `coverage_weighted`: `|A∩B|² / (|A|·|A∪B|)` — поощряет колонки с большим
  числом совпавших триграмм шаблона

### `verify.py` — Построчная верификация

**Выбор anchor-колонки:**
- `select_anchor(col_info)`: предпочитает exact-колонку с nd > 100,
  затем fuzzy с nd > 100, иначе индекс 0

**Быстрый путь (exact anchor):**
- `JOIN unnest(anchor_values) ON norm = value` — точное совпадение
- Для каждой строки шаблона: hash equality для exact колонок,
  `row_similarity ≥ threshold` для fuzzy колонок
- Строка считается совпавшей если ≥80% колонок совпали

**Медленный путь (fuzzy fallback):**
- Если быстрый путь вернул 0 строк и anchor — fuzzy-колонка
- Сканирует первые N строк таблицы (до 10 000)
- `trigram_sim(anchor_value, db_value) ≥ verify_sim_threshold`
- Та же логика построчного сопоставления

### `compare.py` — Данные для сравнения

**Source-driven подход:**
- Источник данных — строки БД (слева), справа — совпавшая строка шаблона
- Выравнивание: anchor-колонка (exact или fuzzy fallback)
- Использует ВСЕ candidate-совпадения, не только 1:1 assigned

**Fuzzy anchor fallback:**
- Если primary anchor — exact (не fuzzy-capable), а strict anchor match
  не дал результата, ищет совпадение через fuzzy-колонку
- Позволяет `--only-matched` сохранять строки с чисто нечёткими совпадениями

**Классификация ячеек (`_classify_pair`):**
- `exact`: нормализованные значения равны
- `fuzzy`: `row_similarity(tmpl_norm, db_norm) ≥ verify_sim_threshold`
- `none`: всё остальное
- Вычисляет `shared_trigram_spans` для подсветки на обеих сторонах

**Фильтрация мёртвых колонок:**
- После построения всех строк удаляет колонки с нулём реальных совпадений
- Обновляет source_columns, target_columns, matched_pairs, и строки

### `visualize.py` — HTML-отчёты

**Все отчёты самодостаточны** (открываются из `file://`).

**report.html (поиск):**
- Данные рендерятся напрямую в HTML (мало строк)
- Сортировка по столбцам, фильтр по имени таблицы, чекбокс «только совпадения»
- Для каждой таблицы: score bar, mapping (1:1), candidates (все совпадения с тегами exact/fuzzy)
- CSS-переменные для цветовой схемы

**compare.html (сравнение):**
- gzip+base64 JSON blob → `DecompressionStream` в браузере
- Виртуальный скроллинг: рендерится только видимый диапазон строк (ROW_H=30px, OVER=14)
- Колонки: row number (sticky) | source columns | divider | target columns
- Первая source-колонка — sticky слева
- Resizable колонки (drag), сохранение ширин в localStorage
- Фильтр: текст + «только совпавшие» + «только несовпавшие»
- Подсветка: exact (зелёный), fuzzy (оранжевый с mark по spans)
- Легенда: все пары с типом, процентами exact/ngram, ★ для anchor

**index.html (batch compare):**
- Таблица: #, таблица (ссылка), оценка, проверено, совпало строк, открыть
- Генерируется для `compare --top N`

### `template.py` — Загрузка шаблона

**canonicalize_cell (Python, минимально):**
- None → None
- bool → "true"/"false"
- float → до 6 знаков, strip trailing zeros (.0 → целое)
- datetime → ISO 8601
- str → strip (None если пустая)

**infer_dtype_group:**
- ≥90% значений парсятся как float → "num"
- ≥90% выглядят как дата (YYYY-MM-DD или ISO) → "date"
- Иначе → "text"

**load_template (основная):**
- openpyxl: read_only=True, data_only=True
- auto-detect header: первая строка — все нечисловые строки → header
- Фильтр: колонки с < min_template_distinct не-null значений пропускаются
- Хэширование через БД: `SELECT norm, h64 FROM unnest(vals)` — **критично**
- N-граммы: build_ngram_hashes на нормализованных значениях (Python)

**load_raw_columns (вспомогательная):**
- Для `--columns all`: загружает ВСЕ колонки без хэширования
- Используется для показа несовпавших колонок шаблона

### `indexer.py` — Индексация

**index_column (одна колонка):**
- Проверяет, не проиндексирована ли уже (пропускает, если не `--force`)
- SQL-статистика: n, nd, min/max/percentile_disc (num/date/ts), avg_len (text)
- Фильтр text по `skip_text_avg_len`
- Стриминг хэшей: `SELECT DISTINCT h64` → fetchmany(50000) → np.sort → .npy
- N-граммы (если fuzzy + text + nd ≤ max_nd): `SELECT DISTINCT norm` → build_ngram_hashes → .ngrams.npy
- ColumnRecord → catalog.db

**index_tables (оркестратор):**
- crawl_columns → список колонок
- ThreadPoolExecutor(max_workers) — каждая worker создаёт своё соединение
- tqdm progress bar с logging_redirect_tqdm
- Сбор failed колонок, логгирование ошибок

### `catalog.py` — Обход схемы

- `expand_table_patterns(tables)`: fnmatch → SQL LIKE, запрос к `information_schema.tables`
- `crawl_columns(tables)`: `information_schema.columns` → ColumnInfo[]
- Фильтры: exclude_columns (точные имена), exclude_column_patterns (fnmatch на имя колонки), dtype_groups
- `TYPE_MAPPING`: PostgreSQL типы → dtype_group
- `EXCLUDED_TYPES`: bytea, json, jsonb, xml, geometry, geography, array, user-defined

## Конфигурация

Загружается через `Config.from_yaml(path)`. Ключевые параметры:

| Параметр | По умолчанию | Описание |
|----------|-------------|----------|
| dsn | — | PostgreSQL connection string (Greenplum 6.24) |
| tables | [] | Список fnmatch-паттернов таблиц |
| store_type | local | local (sqlite+.npy) или db (Greenplum tablefp_columns) |
| index_dir | ./fp_index | Директория локального индекса |
| max_workers | 4 | Потоков для параллельной индексации |
| min_containment | 0.3 | Порог containment для pairing/scoring |
| candidate_min_containment | =min_containment | Порог для отображаемых candidates (не влияет на scoring) |
| min_template_distinct | 5 | Минимум уникальных значений в колонке шаблона |
| skip_text_avg_len | 500 | Пропускать text колонки с высокой средней длиной |
| fuzzy.enabled | false | Включить Phase 2 (n-граммное сравнение) |
| fuzzy.ngram_size | 3 | Размер n-грамм |
| fuzzy.alpha | 0.8 | Вес n-граммного containment: S = max(exact, α·ngram) |
| fuzzy.metric | jaccard | Метрика: jaccard или coverage_weighted |
| fuzzy.verify_sim_threshold | 0.4 | Порог trigram_sim для строковой верификации |

## CLI команды

```
tablefp index --config config.yaml [--force] [--tables ...] [-v]
tablefp search template.xlsx --config config.yaml [--top 5] [--no-verify] [--fuzzy] [--out results.json]
tablefp compare template.xlsx --config config.yaml --table schema.name [-o compare.html] [--fuzzy]
tablefp compare template.xlsx --config config.yaml --top 10 [-o compare_reports/] [--fuzzy]
tablefp report results.json [-o report.html]
tablefp report r1.json r2.json -o merged.html
```

Прямой запуск без установки: `python run_tablefp.py <command> ...`

## Тестирование

```bash
# Юнит-тесты (38 тестов, БД не нужна)
pytest tablefp/tests/

# Интеграционные тесты (требуют POSTGRES_DSN)
pytest tablefp/tests/test_norm.py -m integration
```

Маркер `integration` зарегистрирован в `conftest.py`.
Фикстуры: `db_dsn` (читает POSTGRES_DSN), `skip_if_no_db`.

### Что покрывают тесты

| Файл | Покрытие |
|------|---------|
| test_hashing.py | h64 (совместимость с SQL), ngrams (padding, edge cases), trigram_sim, row_similarity, build_ngram_hashes |
| test_template.py | canonicalize_cell (все типы), infer_dtype_group (num/date/text/empty) |
| test_matcher.py | is_compatible (все пары), containment (perfect/partial/none), Hungarian (perfect/missing/below-threshold), candidates (multiple db cols, fuzzy inclusion, jaccard anti-sponge, candidate_min_containment, coverage_weighted priority, duplicate prevention) |
| test_norm.py | SQL-нормализация (num vectors, text case/whitespace/null, date, ts, bool), h64 hash stability |
| test_compare.py | shared_trigram_spans, classify_pair (exact/fuzzy/none/disabled), build_comparison (matched/all columns mode, source-driven, candidates, dead column filtering, fuzzy anchor fallback, exact column fuzzy row matching, limit/no-limit) |
| test_visualize.py | report (total_scanned, default —, merge), compare_index (links, сортировка) |

## Важные соглашения и ограничения

1. **Нормализация только в SQL.** Хэши шаблона вычисляются через `unnest()` в БД,
   а не в Python — это гарантирует идентичность хэшей с проиндексированными.

2. **Стабильность хэшей.** `h64('abc')` всегда равен `-8070080442485551184`.
   Менять алгоритм хэширования нельзя без полной переиндексации.

3. **Конфиденциальность.** `config/` в `.gitignore`, `config.yaml` содержит
   реальные DSN с паролями. `config.example.yaml` — шаблон без credentials.

4. **mmap-загрузка.** `.npy` файлы загружаются через `mmap_mode="r"` —
   эффективно для больших массивов, не загружает всё в память.

5. **Параллельность.** Индексация использует `ThreadPoolExecutor` (I/O-bound).
   Каждый worker создаёт своё psycopg2-соединение.

6. **PG 9.4 совместимость.** `md5()`, `substr()`, `::bit(64)::bigint` —
   работают в Greenplum 6.24. `percentile_disc` доступен в GP 6.

7. **Source-driven compare.** Строки БД — источник истины; строки шаблона
   подбираются к ним, а не наоборот. Это позволяет показывать ВСЕ строки БД.

8. **Скоринг.** Формула `Σ(S·W)/Σ(W) × coverage` отдаёт предпочтение таблицам
   где больше колонок шаблона нашли соответствие (штраф за неполноту).

9. **Fuzzy metric выбор.** `jaccard` для случаев где важна симметричность;
   `coverage_weighted` когда длинные free-text колонки (например, "division")
   должны ранжироваться выше коротких шумных совпадений.

10. **HTML-отчёты самодостаточны.** Содержат inline CSS и JS, открываются из
    `file://`. Compare-отчёт использует gzip+base64 для упаковки данных и
    виртуальный скроллинг для производительности.

## Зависимости

- Python 3.10+
- Greenplum 6.24 / PostgreSQL 9.4+
- `psycopg2-binary` — драйвер PostgreSQL
- `numpy` — массивы хэшей, np.isin, mmap-загрузка
- `scipy` — Hungarian algorithm (linear_sum_assignment)
- `openpyxl` — чтение xlsx (read_only, data_only)
- `pyyaml` — парсинг конфигурации
- `click` — CLI framework
- `tqdm` — progress bar при индексации
