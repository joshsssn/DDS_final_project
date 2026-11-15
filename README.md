# Landmarks of the Wikipedia game

> Are the pages that Wikipedia-game players use as landmarks (countries, general pages,
> link-rich pages) actually the best hubs for connecting any two Wikipedia articles?
> And if not, which pages are?

Group: Raphaël HAZAN, Nicolas SALAPETE, Edouard SOUIED, Josh SOUSSAN —
*Data, Data Storage, Data Collection* (CentraleSupélec / Université Paris-Saclay).

The project builds the full directed link graph of French Wikipedia from the official
database dumps (pinned date **2026-10-01**). On random article pairs it measures how good each
candidate page is as a hub (mean detour, share of pairs on a shortest path), and compares this
"real" quality with the "perceived" landmark criterion (out-degree and page type).

## Reproduce everything

Requirements: [uv](https://docs.astral.sh/uv/) (it installs Python 3.13 itself), ~20 GB of free
disk and ≥ 16 GB of RAM (developed on 32 GB). No database server, Docker or `make` is needed.

```bash
uv sync                          # creates .venv from uv.lock (pinned versions)
uv run python run.py all         # download -> parse -> clean -> store -> analyze -> robustness -> bias -> figures -> numbers -> notebook -> test
```

`run.py` runs the steps in order and skips a step whose output already exists (`--force` redoes it).
Individual steps:

| Step | Command | Output | Time on our laptop |
|------|---------|--------|--------------------|
| download | `uv run python run.py download` | `data/raw/*.sql.gz` (2.76 GB), SHA-1 verified, `manifest.json` | ~10 min (network) |
| parse | `uv run python run.py parse` | `data/interim/*.parquet`, `parse_stats.json` | 379 s (277 s for the 470 M `pagelinks` rows) |
| clean | `uv run python run.py clean` | `data/processed/graph/` (CSR arrays + nodes), `quality.json` | ~6 min, mostly sorting 236 M edges |
| store | `uv run python run.py store` | `reports/generated/storage_benchmark.json` | a few minutes |
| analyze | `uv run python run.py analyze` | `reports/generated/sample_seed42_n200000/` | ~6 min for the 200,000 pairs + ~12 min for the hubs (first run; cached afterwards) |
| robustness | `uv run python run.py robustness` | `reports/generated/sample_seed69_n200000/` (independent second sample, `analysis.robustness_seed`) | ~6 min + new hubs (first run) |
| bias | `uv run python run.py bias` | `reports/generated/bias_check.{csv,json}`, API cache `data/raw/api/` | ~10 min the first time (one request every 3 s, 200 pages), then cached |
| figures | `uv run python run.py figures` | `reports/figures/*.png|pdf` | seconds |
| numbers | `uv run python run.py numbers` | `reports/generated/numbers.tex` (LaTeX macros used by the report) | seconds |
| notebook | `uv run python run.py notebook` | `notebooks/final_analysis.ipynb` (executed) | ~1 min |
| test | `uv run python run.py test` | unit tests of the parser and BFS | seconds |

Then compile the report and the slides inside `report/`: `latexmk -pdf report.tex` and
`latexmk -pdf slides.tex` (or upload `report/` together with `reports/figures/` and
`reports/generated/` to Overleaf). The slides are a 16:9 Beamer deck; they need the Fira Sans
font package (`fira`, included in MiKTeX and TeX Live).

### New samples without redoing anything

All BFS results are cached in `data/processed/cache/`:
- every evaluated pair `(s, t)`;
- the full forward and reverse distance vectors of every hub.

To evaluate another sample:

```bash
uv run python -m wikigame.analysis --seed 43 --n-pairs 200000
```

A new seed draws pairs that were never used by a previous sample, and only new pairs or new
candidate hubs are computed. Results go to `reports/generated/sample_seed43_n200000/`. Increasing
`--n-pairs` with the same seed extends the existing sample.

## Bonus: interactive path finder

```bash
uv run python bonus/server.py --open     # starts the server and opens http://localhost:8000
```

On Windows, double-clicking `Bonus.bat` at the root does the same.

A small local page on top of the cleaned graph (it needs `data/processed/graph/`). Type a start
and a target article: two searches run at once, one from each end, and the page replays them as
an animated graph until they meet on a shortest path. It uses the same bidirectional BFS as the
analysis and answers in a few milliseconds.

The two fields search as you type, over article titles and Wikipedia's redirect titles. The
search ignores accents and case, finds a word inside a title, and proposes close spellings. A
title that does not exist is shown in red, like a red link. The first start builds this search
index (about 40 s); later starts take a few seconds.
It is not part of the graded pipeline and `run.py` does not run it.

## Repository layout

```
config.yaml            dump date, paths, seeds, sample sizes (single source of parameters)
run.py                 pipeline entry point
pyproject.toml, uv.lock, requirements.txt   pinned environment
src/wikigame/
  download.py          download + resume + SHA-1 check
  parse_sql.py         streaming parser of MySQL dump files -> Parquet
  clean.py             quality measurements + cleaning -> CSR graph
  graph.py             CSR structure, BFS, shortest-path sampling
  storage_benchmark.py numpy CSR vs DuckDB on our queries
  analysis.py          descriptive stats, candidate hubs, detour metrics, perceived vs real
  bias_check.py        MediaWiki API: template inflation of out-degree
  figures.py           report figures
  slide_figures.py     figures sized for the 16:9 slides (run by the `figures` step)
  report_numbers.py    LaTeX macros with every number quoted in the report
tests/                 parser and BFS unit tests (toy inputs)
bonus/                 interactive shortest-path explorer (local web page, standard library only)
labels/hub_type_labels.csv   type label of each candidate hub (assigned by hand from the page titles)
notebooks/             final reproducible notebook
docs/                  provenance, decision log, data quality assessment, storage comparison
report/                LaTeX report (report.tex/pdf), Beamer slides (slides.tex/pdf),
                       one-page data quality review (data_quality_review.tex/pdf)
reports/figures/       generated figures;  reports/generated/  generated tables & numbers
data/                  (not versioned) raw / interim / processed data
```

## Licence of the data

Wikipedia content and metadata: CC BY-SA 4.0 / GFDL (Wikimedia Foundation). The dumps contain no
personal data beyond public page metadata.
