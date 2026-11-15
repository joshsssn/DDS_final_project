# Data quality assessment and cleaning

Every number below is produced by code:
- `src/wikigame/clean.py` writes `data/processed/quality.json`;
- `src/wikigame/parse_sql.py` writes `data/interim/parse_stats.json`.

Percentages were computed from those files. Dump: frwiki **20261001**.

The method is to **measure first, then decide**. Each transformation below is tied to a measured
problem and to how that problem would affect our use, which is shortest-path distances between
articles in a click game.

## 0. Parsing completeness

| Table | Rows parsed | Rows found by independent `zcat \| grep -c '^('` | Rows kept |
|---|---:|---:|---:|
| page | 14,148,116 | 14,148,116 | 14,148,116 |
| pagelinks | 470,370,087 | 470,370,087 | 470,370,087 |
| linktarget | 31,672,724 | 31,672,724 | 31,672,724 |
| redirect | 2,071,832 | 2,071,832 | 2,071,832 |
| page_props | 8,480,889 | 8,480,889 | 180,169 (`disambiguation` rows only) |

The parser loses no rows. The independent count is in `data/interim/independent_row_counts.txt`.

## 1. Pages: namespaces, redirects, disambiguation

- There are 14,148,116 pages in 28 namespaces.
- **Namespace 0 (main)** has 4,627,118 pages, of which **1,844,607 are redirects**. This leaves
  **2,782,511 articles**.
- The largest other namespaces are:
  - User talk (3): 4,743,201 pages
  - Talk (1): 2,729,309
  - Category (14): 723,313
  - User (2): 581,713
  - Template (10): 264,935
- **Disambiguation pages**: 180,169 pages are flagged. 180,041 of them are articles (6.47% of the
  articles), 82 are namespace-0 redirects, and 0 point to a missing page. The remaining 46
  (by subtraction) are pages outside namespace 0.

*Effect on our use.* More than half of all pages are not articles. Keeping them would add nodes
that a player cannot visit.

*Decision.* Nodes are namespace-0 non-redirect pages only. Disambiguation articles stay as
traversable nodes but are never drawn as start or target.

## 2. Redirects

The table counts the 1,844,607 namespace-0 redirect pages by outcome:

| Outcome | Count |
|---|---:|
| resolved to an article | 1,844,454 |
| redirect to another namespace | 149 |
| interwiki redirect | 3 |
| broken (target does not exist) | 1 |
| redirect cycles | 0 |
| missing row in `redirect` table | 0 |

- **Chain lengths** among resolvable redirects:
  - 1 hop: 1,844,357
  - 2 hops: 91
  - 3 hops: 6
- That makes **97 double redirects** and no chain longer than 3.
- 62,693 redirects (3.4%) point to a section (`#fragment`). The fragment is ignored: the click
  still lands on the target article.

*Effect on our use.* 10.7 M links from articles point to a redirect. Dropping them would remove
real clickable paths, and keeping redirects as nodes would add a fake extra click.

*Decision.* Link targets that are redirects are replaced by their final article. Chains are
followed up to `max_redirect_depth = 5` (config). The measured maximum is 3, so no chain is cut.
Cycles (none found), broken redirects and redirects that leave namespace 0 are dropped.
- Note: MediaWiki itself follows only one hop when displaying a page. For the 97 double
  redirects, a real player would land on an intermediate redirect page and need one more click.
- These double redirects concern so few links that the choice of depth does not change results.

## 3. Links (`pagelinks`, 470,370,087 rows)

| Source of the link | Rows | Share |
|---|---:|---:|
| article (namespace 0, not a redirect) | 282,944,432 | 60.15% |
| page of another namespace | 185,569,438 | 39.45% |
| redirect page | 1,856,217 | 0.39% |
| source page missing from `page` | 0 | 0% |

For the 282,944,432 links **from articles**, the target is:

| Target | Rows | Share |
|---|---:|---:|
| an article | 228,032,774 | 80.59% |
| a redirect resolved to an article | 10,729,947 | 3.79% |
| a non-existent page (**red link**) | 27,000,122 | 9.54% |
| a page of another namespace (category, template, file…) | 17,180,988 | 6.07% |
| a redirect to another namespace / interwiki | 601 | 0.00% |
| a `pl_target_id` missing from `linktarget` | 0 | 0% |

Other observations:
- `linktarget` contains 31,672,724 targets. 8,972,646 of them (28.3%) are namespace-0 titles
  with no page, i.e. red links.
- For 963 links, `pl_from_namespace` disagrees with the namespace of the source page in `page`.
  These are pages moved between namespaces after the link table was updated. We trust the `page`
  table, which tells us whether a page is an article.

*Effect on our use.* Red links and links to other namespaces cannot be followed to an article in
the game, so keeping them would create impossible moves. Links from other namespaces (talk
pages, templates…) are not moves either.

*Decision.* Keep only article → (article or resolved redirect) links: **238,762,721 candidate edges**.

## 4. After redirect resolution: self-loops and duplicates

| | Count | Share of candidate edges |
|---|---:|---:|
| self-loops (source = target after redirect resolution; direct self-links and redirect-induced ones are not counted separately) | 53,339 | 0.022% |
| duplicates (page links both to X and to a redirect of X) | 2,806,457 | 1.18% |
| **final edges** | **235,902,925** | |

*Decision.*
- Self-loops are removed: a click to the same page is never part of a shortest path.
- Duplicates are removed: the graph is a simple directed graph, and the out-degree counts
  *distinct* linked articles.

## 5. Degrees of the cleaned graph (2,782,511 nodes, 235,902,925 edges)

**Out-degree.** Mean 84.8, median 40, maximum 5,374.
- The maximum is "Liste des cantons français au Grand Est depuis 2015".
- 256 sinks have out-degree 0 (0.009%).

**In-degree.** Median 14, maximum 579,086.
- The maximum is "Autorité (sciences de l'information)", linked by the authority-control template.
- 46,349 orphans have in-degree 0 (1.67%). They cannot be reached as a target.
- 133 articles are isolated (both degrees 0).

*Effect on our use.* Sinks cannot start a game and orphans cannot end one. Pairs involving them
are unreachable.

*Decision.* They are **kept** and reported. Pairs without a path are counted and reported in the
analysis, never silently dropped. Removing them would hide a real property of the graph.

The degree extremes already show **template effects**:
- The most-linked article is reached through the authority-control template.
- "International Standard Book Number" (535,561 in-links) is reached through citation templates.
- The pages with the highest out-degree are long lists.

The bias check (`bias_check.py`) quantifies how much navigation templates inflate the out-degree.
We do **not** remove template links from the graph: a player can click them, so they are real
moves of the game. We only document the distortion and measure it.

## 6. What we did not change

- No sampling of the graph, no imputation, no artificial noise.
- Titles are kept exactly as in the dump (UTF-8, underscores).
- Link targets that are redirects to a section keep only the article.
