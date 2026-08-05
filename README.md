# 🔍 tablefp — поиск таблиц по xlsx-шаблону

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://python.org)
[![Greenplum](https://img.shields.io/badge/Greenplum-6.24-green)](https://greenplum.org)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Дано: небольшой xlsx-шаблон (10–100 строк). Задача: найти, в каких таблицах
Greenplum содержатся похожие данные.

Два режима сопоставления:

| Фаза | Метод | Устойчивость |
|------|-------|-------------|
| **Phase 1** — exact | 64-битные MD5-хэши значений | Только точные совпадения |
| **Phase 2** — fuzzy | N-граммное (триграммное) сравнение | Опечатки, мелкие правки, подстроки |

## 🚀 Быстрый старт

```bash
# Клонируем
git clone https://github.com/Rail-Akhm/table_similarity.git
cd table_similarity

# Конфигурируем
cp config.example.yaml config/config.yaml
# Редактируем config/config.yaml — указываем DSN и паттерны таблиц

# Индексируем
python run_tablefp.py index --config config/config.yaml

# Ищем
python run_tablefp.py search шаблон.xlsx --config config/config.yaml --top 10 --fuzzy
```

Или через pip:

```bash
pip install -e .
tablefp index --config config/config.yaml
tablefp search шаблон.xlsx --config config/config.yaml --top 10 --fuzzy
```

## 📋 Команды

### `index` — индексация таблиц

Строит отпечатки колонок (хэши + статистика) для таблиц из конфига. Запускается
один раз, затем — только при появлении новых таблиц или обновлении данных.

```bash
tablefp index --config config/config.yaml           # все таблицы из конфига
tablefp index --config config/config.yaml --force   # переиндексировать всё
tablefp index --config config/config.yaml -v         # подробный лог
tablefp index --config config/config.yaml --tables "dwh.orders" --tables "stage.*"
```

**Что делает для каждой колонки:**

1. Считает статистику: `n`, `nd` (уникальных), min/max/квантили (числа) или avg_len (текст)
2. Стримит `SELECT DISTINCT h64` → сортированный массив → `.npy`
3. Если включён fuzzy и колонка текстовая: строит n-граммные хэши → `.ngrams.npy`
4. Пишет метаданные в `catalog.db` (SQLite)

**Результат:** каталог `fp_index/` с `.npy`-файлами и `catalog.db`.

| Флаг | Описание |
|------|----------|
| `--force` | Переиндексировать уже обработанные колонки |
| `--tables PAT` | Ограничить паттернами (можно несколько раз) |
| `-v`, `--verbose` | Подробный лог: каждая колонка, время, ошибки |

---

### `search` — поиск совпадений

Ищет таблицы, похожие на xlsx-шаблон, среди всех проиндексированных.

```bash
tablefp search шаблон.xlsx --config config/config.yaml --top 10 --fuzzy
tablefp search шаблон.xlsx --config config/config.yaml --top 5 --out results.json
tablefp search шаблон.xlsx --config config/config.yaml --no-verify
tablefp search шаблон.xlsx --config config/config.yaml --fuzzy --no-fuzzy  # принудительно вкл/выкл
```

| Флаг | Описание |
|------|----------|
| `--top N` | Показать N лучших результатов (по умолчанию: 5) |
| `--no-verify` | Пропустить построчную верификацию (быстрее) |
| `--no-header` | В шаблоне нет строки заголовка |
| `--sheet NAME` | Имя листа (по умолчанию: первый) |
| `--out FILE` | Сохранить результаты в JSON |
| `--fuzzy` / `--no-fuzzy` | Вкл/выкл нечёткое сравнение (по умолчанию: из конфига) |
| `--min-distinct N` | Переопределить порог уникальных значений |
| `-v`, `--verbose` | Подробный лог |

**Формат вывода:**

```
 1. dwh.orders
    Score: 0.8523
    Verified rows: 92.5%
    Column mapping:
      name     → full_name       containment=92.3%  unique=512 340
      city     → region          containment=61.4%  unique=1 205
    All matches (exact/fuzzy):
      [exact] name → full_name   exact=92.3%  fuzzy=95.1%  unique=512 340
      [fuzzy] city → region      exact=12.0%  fuzzy=61.4%  unique=1 205
```

Для каждой таблицы показывается:
- **mapping** — 1:1 назначенные пары колонок (используются в scoring)
- **candidates** — все пары выше порога, включая те, что Hungarian-алгоритм не выбрал

---

### `compare` — построчное сравнение

Показывает строки таблицы БД рядом со строками шаблона. Совпадения подсвечены:
- 🟢 **зелёным** — точное совпадение (нормализованные значения равны)
- 🟠 **оранжевым** — нечёткое совпадение (общие триграммы выделены в тексте)

```bash
# Одна таблица
tablefp compare шаблон.xlsx --config config/config.yaml \
    --table dwh.orders --fuzzy -o compare.html

# Топ-10 таблиц — отчёты сравнения для каждой + index.html
tablefp compare шаблон.xlsx --config config/config.yaml \
    --top 10 --fuzzy -o compare_reports/
```

| Флаг | Описание |
|------|----------|
| `--table SCHEMA.TABLE` | Конкретная таблица (single-table mode) |
| `--top N` | Топ-N таблиц по score (batch mode) |
| `--limit N` | Макс. строк для сравнения (0 = без ограничений) |
| `--columns all\|matched` | Все колонки или только совпавшие |
| `--only-matched` | Только строки с хотя бы одним совпадением |
| `--only-hit-cols` / `--no-only-hit-cols` | Скрыть колонки без реальных совпадений |
| `--all-template-cols` / `--no-all-template-cols` | Показать все колонки шаблона |
| `--no-verify` | Пропустить верификацию строк |
| `--sheet NAME` | Имя листа шаблона |

---

### `report` — HTML-отчёт поиска

Превращает JSON из `search --out` в самодостаточный HTML с сортировкой,
фильтром и полосами оценок.

```bash
tablefp report results.json -o report.html
tablefp report run1.json run2.json -o merged.html   # объединить несколько запусков
tablefp report results/*.json -o merged.html         # glob-паттерны
```

---

### `run` — полный цикл одной командой

```bash
tablefp run шаблон.xlsx --config config/config.yaml --top 10 --fuzzy -o run_reports/
```

Выполняет всё сразу: search → search.json → report.html → compare-отчёты для топ-N таблиц → index.html.

## ⚙️ Конфигурация

```yaml
# ── Подключение ──────────────────────────────────────────────────────────
dsn: "postgresql://user:password@host:5432/dbname"

# ── Таблицы ──────────────────────────────────────────────────────────────
# fnmatch-паттерны: * = любые символы, ? = один символ
tables:
  - "dwh.*"              # все таблицы схемы dwh
  - "stage.orders"       # конкретная таблица
  - "dp_rid_*.*"         # все таблицы в схемах dp_rid_*

# ── Фильтры колонок ─────────────────────────────────────────────────────
exclude_columns: []                       # точные имена: "schema.table.column"
exclude_column_patterns:                   # fnmatch на имя колонки
  - "s__*"                                # служебные колонки
  - "wf_load_*"                           # колонки загрузки
# dtype_groups: ["text"]                  # индексировать только text

# ── Хранилище ───────────────────────────────────────────────────────────
store_type: "local"            # local (sqlite+.npy) или db (Greenplum-таблица)
index_dir: "./fp_index"        # каталог локального индекса
# storage_dsn: "..."           # для DB-хранилища (по умолчанию = dsn)

# ── Производительность ───────────────────────────────────────────────────
max_workers: 4                 # одновременных соединений с БД
skip_text_avg_len: 500         # пропускать text-колонки со средней длиной > N

# ── Сопоставление ────────────────────────────────────────────────────────
min_containment: 0.3            # порог containment для pairing/scoring
candidate_min_containment: 0.3  # порог для candidates-списка (не влияет на score)
min_template_distinct: 5        # мин. уникальных значений в колонке шаблона

# ── Нечёткое сравнение (Phase 2) ─────────────────────────────────────────
fuzzy:
  enabled: true
  ngram_size: 3                 # размер n-грамм (как pg_trgm)
  max_nd: 2000000               # макс. unique для n-граммной индексации
  columns: []                   # whitelist колонок (fnmatch), пусто = все text
  alpha: 0.8                    # S = max(exact, α × ngram)
  metric: jaccard               # jaccard или coverage_weighted (см. ниже)
  verify_sim_threshold: 0.4     # порог trigram_sim для строковой верификации
```

### Метрики fuzzy

| Метрика | Формула | Когда использовать |
|---------|---------|-------------------|
| `jaccard` | `|A∩B| / |A∪B|` | По умолчанию. Симметричная, устойчива к «губке» (длинный текст с кучей триграмм) |
| `coverage_weighted` | `|A∩B|² / (|A|·|A∪B|)` | Когда важны колонки с *большим числом* попаданий триграмм шаблона (например, название месторождения как подстрока длинного description) |

## 🧠 Как это работает

### Индексация

```
Config → crawl_columns (information_schema) → ColumnInfo[]
→ ThreadPoolExecutor → index_column():
  1. SQL: COUNT, COUNT DISTINCT, min/max/percentile_disc (или avg_len для text)
  2. SQL: SELECT DISTINCT h64 → np.fromiter чанками → np.sort → .npy (mmap)
  3. Если fuzzy+text: SELECT DISTINCT norm → add_ngram_hashes() → .ngrams.npy
  4. ColumnRecord → catalog.db (SQLite)
```

### Поиск

```
xlsx → load_template():
  1. openpyxl → canonicalize_cell → infer_dtype_group (num/date/text)
  2. SQL: unnest(значения) → (norm_v, h64) — нормализация ТОЛЬКО в БД
  3. fuzzy+text: build_ngram_hashes() на SQL-нормализованных значениях

→ Для каждой таблицы в индексе:
  Stage 0: prefilter (dtype-совместимость + пересечение numeric-диапазонов)
  Stage 1: containment matrix S[i,j] = max(exact, α × ngram)
  Stage 2: Hungarian (scipy) + scoring = Σ(S·W)/Σ(W) × coverage
  Stage 3: verify_rows (anchor-join, ≥80% колонок)

→ TableMatch { score, mapping, candidates }
```

### Скоринг

```
W[j]   = log2(nd_j + 1)               # вес селективности колонки
pairs  = hungarian(max S[i,j] × W[j])  # оптимальное назначение
score  = Σ(S[p] × W[p]) / Σ(W[p])      # средневзвешенное
score  *= matched / total              # штраф за неполноту покрытия
```

### Сравнение

```
Source-driven: строки БД — источник, строки шаблона подбираются
Выравнивание: anchor-колонка (exact) + fuzzy fallback
_classify_pair: exact (норм. равны) | fuzzy (row_similarity ≥ threshold) | none
Подсветка: shared_trigram_spans — общие триграммы на обеих сторонах
```

## 📊 Формат отчётов

Все HTML-отчёты **самодостаточны** — открываются из `file://`, не требуют
интернета. Содержат inline CSS и JavaScript.

| Отчёт | Формат | Особенности |
|-------|--------|------------|
| `report.html` | Таблица с результатами поиска | Сортировка, фильтр, score bar, exact/fuzzy теги |
| `compare.html` | Построчное сравнение | Виртуальный скроллинг, resizable колонки, gzip+base64 blob |
| `index.html` | Индекс сравнений | Список таблиц со ссылками на отдельные отчёты |

## 🔧 Нормализация и хэширование

**Критическое правило: вся нормализация и хэширование — только через SQL.**
Никогда не нормализуй значения в Python.

| Тип | SQL-нормализация | Хэш |
|-----|-----------------|-----|
| text / uuid | `lower(btrim(regexp_replace(x, '\s+', ' ', 'g')))` | `('x' \|\| substr(md5(norm), 1, 16))::bit(64)::bigint` |
| num | round до 6 зн., обрезка нулей | md5 → int64 |
| date | `to_char(x, 'YYYY-MM-DD')` | md5 → int64 |
| timestamp | `to_char(x, 'YYYY-MM-DD"T"HH24:MI:SS')` | md5 → int64 |
| bool | `CASE WHEN x THEN 'true' ELSE 'false' END` | md5 → int64 |

## 📦 Зависимости

| Пакет | Версия | Назначение |
|-------|--------|-----------|
| Python | ≥ 3.10 | – |
| Greenplum / PostgreSQL | 6.24 / 9.4+ | База данных |
| `psycopg2-binary` | – | Драйвер PostgreSQL |
| `numpy` | – | Массивы хэшей, `np.isin`, mmap |
| `scipy` | – | Hungarian algorithm |
| `openpyxl` | – | Чтение xlsx |
| `pyyaml` | – | Конфигурация |
| `click` | – | CLI |
| `tqdm` | – | Progress bar |

## 🧪 Тестирование

```bash
pytest tablefp/tests/                              # 72 юнит-теста
pytest tablefp/tests/test_norm.py -m integration   # 9 интеграционных (нужен POSTGRES_DSN)
```

## 📖 Примеры

Пошаговые примеры на русском — [EXAMPLES.md](EXAMPLES.md).
