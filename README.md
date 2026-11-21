# Landmarks of the Wikipedia game

> Are the pages that Wikipedia-game players use as landmarks (countries, general pages,
> link-rich pages) actually the best hubs for connecting any two Wikipedia articles?
> And if not, which pages are?

Group: Raphaël HAZAN, Nicolas SALAPETE, Edouard SOUIED, Josh SOUSSAN —
*Data, Data Storage, Data Collection* (CentraleSupélec / Université Paris-Saclay).

Repository: <https://github.com/joshsssn/DDS_final_project>

**Deliverables:** [report (5 pages)](report/report.pdf) ·
[decision record (2 pages)](report/decision_record.pdf) ·
[slides](report/slides.pdf) · [data quality review](report/data_quality_review.pdf) ·
[final notebook](notebooks/final_analysis.ipynb)

The project builds the full directed link graph of French Wikipedia from the official
database dumps (pinned date **2026-10-01**). On random article pairs it measures how good each
candidate page is as a hub (mean detour, share of pairs on a shortest path), and compares this
"real" quality with the "perceived" landmark criterion (out-degree and page type).

## Reproduce everything

Requirements: [uv](https://docs.astral.sh/uv/) (it installs Python 3.13 itself), ~20 GB of free
disk and ≥ 16 GB of RAM (developed on 32 GB). No database server, Docker or `make` is needed.

```bash
uv sync                          # creates .venv from uv.lock (pinned versions)
uv run python run.py all         # download -> parse -> clean -> store -> analyze -> robustness -> bias -> redirects -> figures -> numbers -> notebook -> test
```

`run.py` runs the steps in order and skips a step whose output exists (`--force` redoes it).
Individual steps:

| Step | Command | Output | Time on our laptop |
|------|---------|--------|--------------------|
| download | `uv run python run.py download` | `data/raw/*.sql.gz` (2.76 GB), SHA-1 verified, `manifest.json` | ~10 min (network) |
| parse | `uv run python run.py parse` | `data/interim/*.parquet`, `parse_stats.json` | 379 s (277 s for the 470 M `pagelinks` rows) |
| clean | `uv run python run.py clean` | `data/processed/graph/` (CSR arrays + nodes), `quality.json`, `reports/generated/degree_counts.csv` | ~6 min, mostly sorting 236 M edges |
| store | `uv run python run.py store` | `reports/generated/storage_benchmark.json` | a few minutes |
| analyze | `uv run python run.py analyze` | `reports/generated/sample_seed42_n200000/` | ~6 min for the 200,000 pairs + ~12 min for the hubs (first run; cached afterwards) |
| robustness | `uv run python run.py robustness` | `reports/generated/sample_seed69_n200000/` (independent second sample, `analysis.robustness_seed`) | ~6 min + new hubs (first run) |
| bias | `uv run python run.py bias` | `reports/generated/bias_check.{csv,json}`, API cache `data/raw/api/` | ~10 min the first time (one request every 3 s, 200 pages), then cached |
| redirects | `uv run python run.py redirects` | `reports/generated/redirect_share.{csv,json}`: share of each candidate's in-degree that exists only through redirect resolution (needs `data/interim/` and the graph) | seconds |
| figures | `uv run python run.py figures` | `reports/figures/*.png|pdf` | seconds |
| numbers | `uv run python run.py numbers` | `reports/generated/numbers.tex` (LaTeX macros used by the report) | seconds |
| notebook | `uv run python run.py notebook` | `notebooks/final_analysis.ipynb` (executed) | ~1 min |
| test | `uv run python run.py test` | unit tests of the parser and BFS | seconds |

Then compile the report, the decision record and the slides inside `report/`:
`latexmk -pdf report.tex`, `latexmk -pdf decision_record.tex` and
`latexmk -pdf slides.tex` (or upload `report/` together with `reports/figures/` and
`reports/generated/` to Overleaf). The slides are a 16:9 Beamer deck; they need the Fira Sans
font package (`fira`, included in MiKTeX and TeX Live).

### Figures and report numbers without the dumps

The small outputs of each step (`results.json`, `hubs.csv`, `bias_check.csv`,
`degree_counts.csv`, `quality.json`, `parse_stats.json`, logs) are versioned. On a fresh copy of
the repository, without downloading anything:

```bash
uv sync
uv run python run.py figures numbers     # redraws reports/figures/ and reports/generated/numbers.tex
```

Only the notebook and the analysis steps need the graph built by `download parse clean`.

### If the 2026-10-01 dump is no longer online

Wikimedia keeps only the most recent dumps on `dumps.wikimedia.org`. Older dates stay available
on the mirrors listed at <https://dumps.wikimedia.org/mirrors.html>. For a mirror that keeps the
same `<base>/frwiki/<date>/` layout, set `dump.base_url` in `config.yaml` to the mirror's
address. `download` checks each file against the SHA-1 sums committed in `data/raw/`, so a wrong
or altered file is rejected.

### Data dictionary of the versioned outputs

`reports/generated/sample_seed<seed>_n<pairs>/` (one folder per sample):

| File | Column | Meaning |
|---|---|---|
| `pairs.csv` | `idx` | position of the pair in the sample (the first N pairs of a seed never change) |
| | `s`, `t` | internal node ids of start and target (row number in `data/processed/graph/nodes.parquet`) |
| | `s_page_id`, `t_page_id` | Wikipedia page ids of start and target (stable, usable with the MediaWiki API) |
| | `d` | shortest distance in clicks; **empty when there is no path** from `s` to `t` |
| `hubs.csv` | `node_id`, `page_id`, `title` | the candidate hub |
| | `top_out_degree`, `top_in_degree`, `top_path_frequency`, `random_baseline` | lists the candidate comes from (a page can be in several) |
| | `out_degree`, `in_degree`, `path_frequency` | perceived criterion and discovery criterion |
| | `mean_detour`, `on_path_rate` | real criterion: mean of d(s,h)+d(h,t)−d(s,t) over connected pairs; share of reachable pairs with detour 0 |
| | `pairs_s_cannot_reach_h`, `pairs_h_cannot_reach_t`, `coverage` | pairs where the hub breaks the path, reported separately |
| | `rank_*` | ranks among the candidates (1 = best) |
| `results.json` | | each summary number of the sample (graph, pairs, candidates, correlations, by type) |

Other files: `reports/generated/degree_counts.csv` (number of articles per out- and in-degree
value), `labels/hub_type_labels.csv` (type label of each candidate of the three samples, see
`docs/decisions.md` §15), `data/processed/quality.json` (each cleaning measurement).

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
  redirect_share.py    in-degree that exists only through redirect resolution
  figures.py           report figures
  slide_figures.py     figures sized for the 16:9 slides (run by the `figures` step)
  report_numbers.py    LaTeX macros with every number quoted in the report
tests/                 parser and BFS unit tests (toy inputs)
bonus/                 interactive shortest-path explorer (local web page, standard library only)
labels/hub_type_labels.csv   type label of each candidate hub (assigned by hand from the page titles)
notebooks/             final reproducible notebook
docs/                  provenance, decision log, data quality assessment, storage comparison
report/                LaTeX report (report.tex/pdf), Beamer slides (slides.tex/pdf),
                       one-page data quality review (data_quality_review.tex/pdf),
                       two-page decision record (decision_record.tex/pdf)
reports/figures/       generated figures;  reports/generated/  generated tables & numbers
data/                  (not versioned) raw / interim / processed data
```

## Licence of the data

Wikipedia content and metadata: CC BY-SA 4.0 / GFDL (Wikimedia Foundation). The dumps contain no
personal data beyond public page metadata.
