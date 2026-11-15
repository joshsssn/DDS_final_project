# Decision log

Every non-trivial choice of the project: what we chose, what we rejected, and why.
Numbers quoted here come from files written by the code; the path of the file is given each
time.

## 1. Project setup

| Topic | Decision |
|---|---|
| Machine | Windows 11, 32 GB RAM, Intel i7-11370H (4 cores / 8 threads), ~125 GB free disk. The whole graph fits in memory, so we use an in-memory design. |
| Environment | Python 3.13 managed with **uv** (`pyproject.toml` + `uv.lock`; `requirements.txt` exported from the lock). No system software: no MySQL, no Docker, no Neo4j. |
| Dump date | Most recent complete dump: **20261001** (all 42 jobs `done` in `dumpstatus.json` on 2026-10-04). Pinned in `config.yaml`. |
| Disambiguation pages | Traversable like any article, but **excluded as start/target** of random pairs. |
| Sample sizes | Official sample: N = 200,000 pairs, K = 100 candidates per criterion, 50 random baseline pages. A first run at N = 2,000 is kept in the repository for comparison. Every computed result is cached on disk and reused, and new samples never reuse previous pairs. |
| Type labels | Assigned by hand from the page titles (see §15). |
| MediaWiki API bias check | Kept (see §18). |
| Storage benchmark | NumPy CSR arrays vs DuckDB benchmarked. Neo4j **only discussed**: we wanted the simplest and lightest setup, with no server. |
| Outputs | Everything in English. Report and slides in LaTeX. Scripts are the main entry point, plus a final notebook. |
| Entry point | **No Makefile.** A pure-Python `run.py` does the same job (`uv run python run.py all`). `make` is not installed on Windows by default and it would add a tool for no benefit. |

## 2. Source and User-Agent

All requests use `user_agent` from `config.yaml`: a descriptive string with no personal data.
A group contact (URL or shared address) can be added there. A personal e-mail must not be put in
a public repository.

`download.py` saves the dump's `dumpstatus.json` next to the raw files and stops if any job of
the dump is not `done`. The "42 jobs done" claim of `docs/provenance.md` is therefore backed by a
file and by code, not by a reading of the web page.

## 3. Dump format: multi-line INSERT statements

- **Observation.** In the 20261001 dumps, `INSERT INTO ... VALUES` is alone on a line and
  each row is on its own line (`(..),` … `(..);`). Older dumps had one huge line per statement.
  The first version of the parser assumed the old layout and found 0 rows.
- **Choice.** `parse_sql.insert_statements` rebuilds each statement by concatenating its
  lines. This is safe because raw newlines only occur between rows: newlines inside strings
  are escaped as `\n`. Both layouts are unit-tested.
- **Alternative.** Import into MySQL/MariaDB. Rejected: it adds a system dependency, and the
  custom parser is validated by strict checks and tests.

## 4. Parser strategy (custom streaming parser, not MySQL)

- **Choice.** A custom Python parser streams the gzip file.
  - It reads column names from each file's own `CREATE TABLE`, and a missing column is a hard error.
  - Each row is matched with a regex for exactly `ncols` values.
  - Consecutive rows must be separated by exactly one comma, and every statement must be
    consumed entirely. Otherwise the parser raises an error instead of silently shifting columns.
  - Integer-only statements (pagelinks) take a fast numpy path, validated by a row count. They
    fall back to the strict path on any surprise.
  - Statements are parsed in 6 processes, and Parquet is written in batches.
- **Alternatives.** Import into MySQL/MariaDB (heavy system install, slow import of ~2.8 GB of
  compressed SQL). Or a third-party SQL parser, which is generic and not designed for multi-GB
  streams.
- **Reason.** No system software, reproducible with `uv`, and fast (page table: 14.1 M rows in 40 s,
  all five tables in 379 s; `data/interim/parse_stats.json`).

## 5. Rows kept from `page_props`

`page_props` has one row per (page, property): wikibase item, default sort, page image, and so on.
We only need the `disambiguation` flag, so the parser keeps only those rows. The total row count
is still recorded in `data/interim/parse_stats.json`.

## 6. Keeping `page_latest`

The revision id of each page at dump time is kept. The bias check can then ask the API for the
**exact revision** of the dump (`oldid=`) instead of the current version, so API counts and dump
counts describe the same text.

## 7. DuckDB joins written as pure equi-joins

The first cleaning run stalled for more than 10 minutes on redirect resolution.
- **Cause.** `EXPLAIN` showed a `BLOCKWISE_NL_JOIN` (a nested loop over 2.8 M × 2.8 M rows).
  The `LEFT JOIN … ON` clauses contained extra non-equality conditions, such as
  `coalesce(rd_interwiki,'') = ''`.
- **Choice.** The conditions moved into filtered sub-queries, so every join is a pure equi-join
  (a hash join).
- **Result.** Redirect resolution now takes 2.6 s, and the whole cleaning about 6.5 min, most of
  it sorting 236 M edges.

## 8. Redirect depth: 5 hops, measured maximum 3

- **Measurement.** `quality.json` gives the chain lengths of resolvable redirects: 1,844,357 have
  1 hop, 91 have 2 and 6 have 3. There are no cycles.
- **Choice.** `max_redirect_depth = 5` resolves all of them.
- **Alternative.** Depth 1, which mirrors MediaWiki's display behaviour: a reader who lands on a
  double redirect sees the intermediate redirect page.
- **Reason.** Only 97 redirects are affected, so the choice has no measurable effect. Depth 5
  keeps the graph closest to "where the link really leads".

## 9. Template links are kept in the graph

The authority-control template ("Autorité (sciences de l'information)", 579,086 in-links) and
citation templates (ISBN, DOI) create very high in-degrees.
- **Choice.** These links are kept: they are clickable, so they are legal moves of the game.
- **Alternative.** Remove links generated by templates. The dumps do not record which links come
  from templates, so this would require re-parsing every page's wikitext. That means a different
  source and a much heavier pipeline.
- **How we account for them.** The bias check measures their weight on a sample (§18), and the
  report discusses them as a limitation.

## 10. Graph storage: CSR arrays, and a vectorised BFS

- **Storage.** The cleaned graph is written as two CSR structures (forward and reverse) of
  `int32` indices, plus a Parquet node table. `docs/storage.md` compares this choice with DuckDB
  on our own queries.
- **BFS.** Level-synchronous and vectorised with numpy. Neighbour gathers are capped at 20 M
  edges per chunk to bound memory.
- **Measured cost.** A full BFS takes 3.3–3.4 s on 2.78 M nodes and 236 M edges
  (`storage_benchmark.json`, arrays in RAM, best of 3).
- **Parallelism.** BFS runs in worker processes (`analysis.bfs_workers`, 6 in `config.yaml`).
  The arrays are memory-mapped, so the workers share one copy through the OS page cache.
- **CSR construction.** `graph.csr_from_edges` sorts one combined 64-bit (src, dst) key instead
  of calling `np.lexsort`. Building the forward and reverse CSR from the cleaned edge list takes
  44 s instead of about 6 min (`storage_benchmark.json`). The arrays produced are identical to
  the stored graph (checked with `np.array_equal`), so the graph fingerprint and every cached
  result remain valid.
- **Alternatives.**
  - `scipy.sparse.csgraph`: Dijkstra for unweighted distances, and its `breadth_first_order`
    gives no distances. It also converts the graph to float64, adding about 2 GB.
  - `numba`: extra compiled dependency.
  - `networkx`: Python objects per edge. A rough estimate, not measured, is ~100 bytes × 236 M
    edges ≈ 24 GB, with pure-Python traversal.

## 11. Hub metrics: which pairs enter which statistic

A hub is connected to a pair when the start reaches the hub and the hub reaches the target.
The two cases are reported separately (`pairs_s_cannot_reach_h`, `pairs_h_cannot_reach_t`);
in the official sample both are **0 for all 299 candidates** (`results.json`).

- **Mean and median detour** are computed over the connected pairs.
- **On-shortest-path rate** is computed over all reachable pairs, so a pair the hub cannot
  serve counts as "not on a shortest path".
- **Coverage** (share of connected pairs) is reported for every hub.
- **Coverage rule.** A hub with coverage below `analysis.min_coverage = 0.9` keeps its metrics but
  is **not ranked** by mean detour: a mean over a handful of pairs is not comparable with the
  others. Group statistics report both `n` and `n_rankable`. In the official sample all 299
  candidates pass the threshold.

## 12. Cache validity and atomic writes

The BFS cache stores node ids and distances, so it is only valid for one graph.
`analysis.check_cache` compares a fingerprint (dump date, max redirect depth, node and edge
counts, SHA-1 of the CSR row pointers) with the one stored in the cache, and wipes the cache on
mismatch.

Cache files are written atomically (temporary file then `os.replace`), for hub vectors and for
the pair cache. The pair cache is checkpointed every 20,000 pairs, so an interrupted run keeps
everything computed so far.

## 13. Candidate path frequency: deterministic ties

The path-frequency criterion uses one shortest path per pair, sampled with a random tie-break.
The randomness is seeded per pair: `default_rng([seed, pair_index])`.
- Many nodes tie at low frequencies, so the top-K list is ordered by (frequency desc, node id asc).
- The number of ties at the K-th position is reported in `results.json`.
- Choosing uniformly among predecessors at each step is *not* uniform over all shortest paths.
  This is acceptable because the criterion only serves to *discover* candidates. Every candidate
  is then evaluated with the exact, tie-free metrics (mean detour, on-shortest-path rate).

## 14. Random baseline hubs

In addition to the three candidate criteria, 50 random articles (non-disambiguation, seeded) are
evaluated as hubs.
- **Reason.** The candidates are selected for having high degrees or path frequencies. A rank
  correlation computed only among them suffers from range restriction. The random articles give
  the "ordinary page" reference level of mean detour.
- **Alternative.** Evaluate every article as a hub: 2.6 M BFS runs, months of computation.

## 15. Type labels

- **Grid.** 14 types: `country`, `continent`, `territory` (sub-national region), `city`,
  `locality` (village, building, monument), `period` (year, date, century, war, regime),
  `discipline`, `concept` (general notion, taxon), `person`, `list` (list, chronology, result
  tables), `reference` (pages mostly linked from infoboxes and templates: identifiers, taxonomic
  ranks, units, administrative or astronomical terms), `language`, `organisation`, `other`.
- **Procedure.** Every candidate hub was labelled by hand from its title
  (`labels/hub_type_labels.csv`), then the whole file was re-read once. The second reading changed
  4 labels for consistency: "Histoire du communisme" (period → concept) and three taxa,
  Arthropodes, Araneae and Chordata (reference → concept, like Animal and Oiseau).
- **Limits.** The labels are a judgement on titles, not a measurement. Some types are small, and
  the border between `concept` and `reference` is debatable. The labels never enter a ranking:
  they only group the candidates in the by-type figure and tables.

## 16. Bidirectional BFS for pair distances

- **Problem.** A one-sided BFS from `s` scans most of the graph before it reaches `t`: about 1.9 s
  of single-core time per pair. Sample size was limited by this.
- **Choice.** `graph.bidirectional_distance` searches forward from `s` and backward from `t` by
  whole levels, always expanding the side with fewer edges to scan, and stops when the two searches
  meet. The distance is exact (the argument is in the docstring).
- **Validation.**
  - Unit test: same distance as a naive BFS for every (s, t) of 40 random graphs, and every
    sampled path is a valid shortest path.
  - Real graph: 400 cached pairs recomputed, 0 mismatches (8 of them unreachable).
- **Measured speed.** 6.5 ms per pair on one core, measured on those 400 pairs.
- **What changes.** The sampled shortest path is drawn differently: a random meeting node, then
  random predecessors and successors. Pairs computed before this change (the first 2,000 of seed
  42) keep their original sampled path. Sampled paths only serve to *discover* candidate hubs; the
  hub metrics use exact distances and are unaffected.
- **Alternative kept for hubs.** A hub needs its distance to *every* node, so hubs still use the
  full one-sided BFS (two per hub).

## 17. Sample sizes: 2,000 pairs, then 200,000, then an independent seed

- **First sample.** 2,000 pairs with seed 42. It produced 305 candidates and the first version of
  the figures (`reports/generated/sample_seed42_n2000/`, kept in the repository).
- **Enlargement.** The bidirectional BFS made pair distances about 290× cheaper, so seed 42 was
  extended to 200,000 pairs. Its first 2,000 pairs are the original sample (prefix property), so
  nothing was discarded. 198,000 new pairs took 329 s with 6 processes.
- **Candidates.** The path-frequency criterion became much less noisy: the 100th candidate appears
  307 times, against 4 times before. The candidate set changed accordingly: 299 pages, 27 of them
  new (countries, years, cities). They were labelled with the same grid as the others.
- **Official sample.** `config.yaml` now has `n_pairs: 200000`, and the report, figures and
  notebook use this sample.
- **Independent sample.** The 2,000-pair and 200,000-pair samples of seed 42 are nested, so their
  agreement does not test sampling noise. A second sample of 200,000 pairs with seed 69
  (`analysis.robustness_seed`, step `robustness` of `run.py`) shares no pair with the first, because
  the sampler excludes pairs already used by another seed.
  - Pairs took 332 s; 53 hubs were new (the 50 random baseline pages differ by design, plus 3 new
    candidates: 2024, Estonie, Moscou).
  - 245 candidates were evaluated in both samples. The Spearman correlation of their mean detours
    is 0.99998, and the largest difference in mean detour is 0.005 click.
  - The 15 best hubs are the same pages in the same order. France is first in both: mean detour
    0.92, on a shortest path for 24.5% (seed 42) and 24.3% (seed 69) of the pairs.
  - The rank correlations with degree are the same: +0.28 / +0.29 for out-degree and −0.58 for
    in-degree.
  - Consequence: seed 42 stays the official sample, and the second sample is reported as a
    robustness check.
- **Labels of the second sample.** The 3 new candidates were labelled. The 50 random baseline
  pages of seed 69 are not labelled: type labels are only used for the candidates of the official
  sample.

## 18. Bias check: sample, and the same unit as the dump

- **Question.** `pagelinks` records every link of the rendered page, including links produced by
  navigation boxes ("palettes") and portal banners. The bias check measures how much of an
  article's out-degree comes from them.
- **Sample.** 200 pages: the 100 articles with the highest out-degree, the 50 with the highest
  in-degree ("general" pages such as countries, which match the question better than lists alone)
  and 50 random non-disambiguation articles (seed 7).
- **Unit.** HTML link titles are resolved exactly as in `clean.py` (redirects to their final
  article, non-articles dropped, own page excluded) before counting. Otherwise a link to X and a
  link to a redirect of X would be counted twice and the comparison with the dump would be wrong.
- **Rate.** One request every 3 s (`bias_check.sleep_seconds`), because 1 request/s got HTTP 429
  after 14 pages. `Retry-After` and exponential backoff on 429/5xx, `maxlag=5`. Every response is
  cached in `data/raw/api/`, so a re-run makes no request.
- **Switch.** `bias_check.enabled: false` skips the step entirely.

## 19. Storage benchmark: identical workload for both options

- **Workload.** Fixed and seeded (`analysis.seed`): one BFS source, 1,000 node ids for degree
  lookups, 1,000 title/id lookups. Both options are built from the same cleaned edge list, and the
  build time of each is measured from that list. The DuckDB BFS result is checked against the
  numpy BFS (`bfs_matches_numpy: true`).
- **Timing protocol.** The CSR query timings are the best of 3; the DuckDB timings are a single
  run. The asymmetry is deliberate (DuckDB's BFS cannot be repeated cheaply) and stated in
  `docs/storage.md`: a warm-up effect cannot explain a 2.8–3.6× gap, so the conclusion holds.
- **Result.** `reports/generated/storage_benchmark.json`, discussed in `docs/storage.md`.

## 20. `run.py` step skipping

Only the expensive steps (parse, clean, store) are skipped, and only when their *complete* output
exists: a partial parse is detected by looking for every table in `parse_stats.json`. The cheap or
cached steps (analyze, robustness, bias, figures, numbers, notebook) always run, so the outputs
follow the current config and labels. `--force` redoes any step.

## 21. Figures, tables and report numbers

- **No number typed by hand.** `report_numbers.py` writes every macro of
  `reports/generated/numbers.tex` and the LaTeX tables from the pipeline outputs. Medians of an
  even number of values are printed with their `.5` rather than truncated to an integer.
- **Degree CCDF.** The denominator is all articles, including degree 0; otherwise the curve
  overstates the share of pages above a degree threshold.
- **Observations and interpretations** are kept in separate subsections of the report. The
  by-type statistics cover the 249 non-random candidates of the official sample.

## 22. Notebook

There is one notebook, `notebooks/final_analysis.ipynb`. It starts from the processed data,
re-runs the analysis from the cache and shows the key tables and figures. Exploration was done
with scripts, and its lasting results are `docs/data_quality.md` and this log. The notebook runs
with or without the labels file.

## 23. Beamer slides

- **Choice.** The deck is written in LaTeX Beamer, 16:9: `report/slides.tex` (17 slides and 3
  backup slides).
- **Numbers.** The slides `\input` the same generated macros as the report. No number is typed by
  hand. New macros cover the robustness check, the pair throughput before and after the
  bidirectional BFS (read from the two analysis logs), and the cost of evaluating every pair.
- **Figures.** `slide_figures.py` draws slide-specific figures at their displayed size. It is run by
  the `figures` step.
- **"Every pair" estimate (backup slide).** It is an extrapolation from measured speeds, not a run:
  6.8 × 10¹² ordered pairs; 357 years pair by pair at 601 pairs/s; about 30 days with one full BFS
  per start article (1.0 s each with 6 processes, from the 27 hubs computed in the 200,000-pair run).
- **Throughput comparison caveat.** The first run used 4 processes and the second 6, so the 284×
  wall-clock ratio mixes the algorithm and the process count. Per process the ratio is about 190×.
  On a single core, on 400 pairs, it was measured at about 290×.

## 24. Bonus: interactive path finder

- **What.** `bonus/server.py` and `bonus/index.html`: a local page that finds a shortest path
  between two articles and replays the bidirectional search as an animated graph.
- **Scope.** The brief asks for no web app in the project. This is an optional extra, kept
  outside the pipeline: `run.py` never calls it, and no result of the report depends on it.
- **Choices.** Standard-library HTTP server and a single HTML file with a canvas, so there is no
  new dependency and it works offline. The page draws a *sample* of each explored level (26 pages
  per level and per side) and prints the real number of pages found at each level under the graph.
- **Measured.** A path request takes 8 to 28 ms, of which 2 to 4 ms for the search itself (three
  test pairs).
- **Article search.** The index holds 4,626,965 titles: the 2,782,511 article titles and the
  resolved redirect titles, which lead to their target article. Keys are folded (no accents, no
  case). A query is matched in three steps, most-linked pages first: titles that start with it,
  then titles with a word that starts with it, then close spellings (one letter dropped, two
  swapped, or a mistyped ending). Seven test queries took 2 to 85 ms. The first start builds the
  index in about 40 s and caches it in `data/processed/bonus_index/`.
- **Launcher.** `Bonus.bat` starts the server and opens the browser when it is ready. If the
  port is already in use, it only opens the page.

## 25. Population sizes used in this log

Several counts appear in this document; they do not describe the same set.

| Count | What it is | Where it comes from |
|---:|---|---|
| 2,000 | pairs of the first sample (seed 42), prefix of the official sample | `reports/generated/sample_seed42_n2000/` |
| 200,000 | pairs of the official sample (seed 42) and of the second sample (seed 69) | `config.yaml`, `results.json` |
| 299 | candidate hubs of the official sample (union of the three top-100 lists and 50 random pages) | `results.json` |
| 305 | candidate hubs of the first 2,000-pair sample | `sample_seed42_n2000/results.json` |
| 245 | candidates evaluated in both seeds | `numbers.tex`, macro `SeedN` |
| 385 | hub vectors in the BFS cache (all samples and runs together) | `data/processed/cache/hubs/` |

## 26. Note on measured wall-clock times

The wall-clock timings logged around the first full run include a 55-minute period during which
the laptop was suspended (16:02–16:57); the processes resumed without error and the results are
complete. The figures quoted in this log and in the report are the compute times, not those
wall-clock spans. `data/processed/analysis.log` keeps the raw log of the first run.
