# tablefp — примеры использования

Полный цикл: **сканирование → поиск → сравнение → отчёты**.

Команды запускаются через установленный пакет (`tablefp ...`) или без установки
(`python run_tablefp.py ...`). Ниже — `tablefp`.

## 0. Подготовка

Скопируйте `config.example.yaml` → `config.yaml` и укажите подключение и таблицы:

```yaml
dsn: "postgresql://user:password@host:5432/mydb"

tables:
  - "dwh.*"        # все таблицы схемы dwh
  - "stage.*"      # все таблицы схемы stage

store_type: "local"
index_dir: "./fp_index"

fuzzy:
  enabled: true   # нечёткое сравнение текстовых колонок (опечатки, правки)
```

## 1. Сканирование — `index`

Строит отпечатки колонок (хэши значений + статистика) для таблиц из `tables`.
Запускается один раз, далее — только для новых таблиц.

```bash
# Просканировать все таблицы из конфига
tablefp index --config config.yaml

# Переиндексировать всё (данные обновились)
tablefp index --config config.yaml --force

# Только часть таблиц (перекрывает конфиг) + подробный лог
tablefp index --config config.yaml --tables "dwh.orders" --tables "stage.*" -v
```

**Результат:** каталог `./fp_index` (sqlite + `.npy`; при включённом fuzzy —
ещё и `.ngrams.npy` для текстовых колонок).

## 2. Поиск — `search`

Ищет, в каких просканированных таблицах содержатся данные из xlsx-шаблона.

```bash
# Топ-10 + сохранение в JSON (нужен для HTML-отчёта, см. п. 4)
tablefp search fields.xlsx --config config.yaml --top 10 --fuzzy --out results.json
```

В консоль — оценка таблицы, соответствие колонок (1:1, использованное в
скоринге) и **все совпадения** с пометкой `[exact]`/`[fuzzy]` и обоими
процентами:

```
   All matches (exact/fuzzy):
     [exact] name -> full_name  exact= 92.3%  fuzzy= 95.1%  unique=512,340
     [fuzzy] city -> region     exact= 12.0%  fuzzy= 61.4%  unique=1,205
```

Полезные опции: `--no-verify` (без проверки строк, быстрее), `--no-header`,
`--sheet "Лист2"`.

## 3. Сравнение — `compare`

Построчное сравнение шаблона с таблицей. Сразу строит HTML: слева — совпавшая
строка шаблона, справа — строки БД; точные совпадения подсвечены зелёным,
нечёткие — оранжевым (с выделением общих триграмм). В легенде у каждой пары —
тип (`точное`/`нечёткое`) и проценты `т:X% н:Y%`.

```bash
# Одна таблица
tablefp compare fields.xlsx --config config.yaml --table dwh.orders --fuzzy -o compare.html

# Только совпавшие колонки, больше строк, только совпавшие строки
tablefp compare fields.xlsx --config config.yaml --table dwh.orders \
    --columns matched --limit 1000 --only-matched --fuzzy -o compare.html
```

По умолчанию показываются только колонки, в которых есть хотя бы одно
совпадение ячеек (`--only-hit-cols`, включён по умолчанию). Чтобы увидеть все
колонки обеих таблиц — `--no-only-hit-cols`.

### Топ-N таблиц сразу

Отчёты сравнения для лучших N совпадений автоматически — отсортированы по
оценке от большей к меньшей:

```bash
# В compare_reports/: schema.table.html для каждой + index.html со списком и ссылками
tablefp compare fields.xlsx --config config.yaml --top 10 --fuzzy -o compare_reports/
```

`index.html` — список таблиц с оценкой, процентом верификации и числом
совпавших строк; каждая ссылка открывает отдельный отчёт.

## 4. Всё сразу — `run`

Поиск + отчёт поиска + отчёты сравнения одной командой. Пишет в выходную
директорию `search.json`, `report.html` (отчёт поиска), по одному
`schema.table.html` на таблицу из топ-N и `index.html` (сводка сравнения):

```bash
tablefp run fields.xlsx --config config.yaml --top 10 --fuzzy -o run_reports/
```

Принимает те же опции, что `search`/`compare`: `--limit`, `--columns`,
`--only-matched`, `--only-hit-cols/--no-only-hit-cols`, `--all-template-cols`,
`--no-verify`. Верификация строк выполняется один раз — на этапе поиска.

## 5. Отчёты

### Поиск — `report`

`search --out` пишет JSON, `report` превращает его в самодостаточный
`report.html` (сортировка, фильтр, полосы оценок, все совпадения с пометками
точное/нечёткое, общее кол-во просканированных таблиц):

```bash
tablefp report results.json -o report.html

# Объединить несколько запусков в один отчёт
tablefp report run1.json run2.json -o merged.html
tablefp report results/*.json -o merged.html
```

### Сравнение

Отдельной команды нет — `compare` сразу пишет HTML (см. п. 3).

Все HTML-отчёты самодостаточны (без зависимостей) и открываются из `file://`.

## 6. Сквозной сценарий

```bash
# 1. Разово сканируем таблицы
tablefp index --config config.yaml

# 2. Ищем похожие на шаблон, JSON + HTML-отчёт поиска
tablefp search fields.xlsx --config config.yaml --top 10 --fuzzy --out results.json
tablefp report results.json -o report.html

# 3. Детально сравниваем топ-10 таблиц одним заходом
tablefp compare fields.xlsx --config config.yaml --top 10 --fuzzy -o compare_reports/
```
