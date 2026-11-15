# Provenance

## Primary source: French Wikipedia database dumps

| | |
|---|---|
| Publisher | Wikimedia Foundation — https://dumps.wikimedia.org/frwiki/ |
| Dump date (pinned in `config.yaml`) | **20261001**. It was the most recent complete dump on 2026-10-04: all 42 jobs are `done` in `dumpstatus.json`. `download.py` saves this file as `data/raw/frwiki-20261001-dumpstatus.json` and refuses to continue if any job is not done. |
| Acquired | 2026-10-04, 13:03–13:11 UTC (per-file times in `data/raw/manifest.json`) |
| Acquisition procedure | `src/wikigame/download.py`: HTTPS streaming download with a descriptive User-Agent and resume support (HTTP Range), then SHA-1 verification against the official checksum file `frwiki-20261001-sha1sums.txt` (kept in `data/raw/`). A file is accepted only if its hash matches. |
| Licence | Wikipedia text and metadata: CC BY-SA 4.0 and GFDL. The tables used contain page metadata and links only, no personal data. |
| Re-obtaining the data | `uv run python run.py download`. The URLs below are stable for dated dumps, but Wikimedia keeps only recent dumps on the main server. Older dates remain on the mirrors listed at https://dumps.wikimedia.org/mirrors.html. |

### Files

| File | Size (bytes) | SHA-1 |
|---|---:|---|
| [frwiki-20261001-page.sql.gz](https://dumps.wikimedia.org/frwiki/20261001/frwiki-20261001-page.sql.gz) | 485,960,880 | `bd0394693e31b62a189d91d311d3a1e83367e5df` |
| [frwiki-20261001-pagelinks.sql.gz](https://dumps.wikimedia.org/frwiki/20261001/frwiki-20261001-pagelinks.sql.gz) | 1,784,677,137 | `f5409c486caf367a7d08e7f687303cbdbdf8800b` |
| [frwiki-20261001-linktarget.sql.gz](https://dumps.wikimedia.org/frwiki/20261001/frwiki-20261001-linktarget.sql.gz) | 352,516,801 | `4a2132ca03853930b362db0092a333d9aa3ad846` |
| [frwiki-20261001-redirect.sql.gz](https://dumps.wikimedia.org/frwiki/20261001/frwiki-20261001-redirect.sql.gz) | 28,188,870 | `fed2fe03b81e8a4ab98161b959d7d7fc496fe57b` |
| [frwiki-20261001-page_props.sql.gz](https://dumps.wikimedia.org/frwiki/20261001/frwiki-20261001-page_props.sql.gz) | 104,234,165 | `7ddc40dc0e130399faf4521aa0d4739d255b9ab5` |
| total | 2,755,577,853 | |

### Schemas actually found (read from each file's `CREATE TABLE`) and row counts

| Table | Columns in the dump | Columns kept | Rows |
|---|---|---|---:|
| page | page_id, page_namespace, page_title, page_is_redirect, page_is_new, page_random, page_touched, page_links_updated, page_latest, page_len, page_content_model, page_lang | page_id, page_namespace, page_title, page_is_redirect, page_latest | 14,148,116 |
| pagelinks | pl_from, pl_from_namespace, pl_target_id | all | 470,370,087 |
| linktarget | lt_id, lt_namespace, lt_title | all | 31,672,724 |
| redirect | rd_from, rd_namespace, rd_title, rd_interwiki, rd_fragment | all | 2,071,832 |
| page_props | pp_page, pp_propname, pp_value, pp_sortkey | pp_page, pp_propname (rows with `pp_propname = 'disambiguation'` only: 180,169) | 8,480,889 |

- These schemas match the MediaWiki manual:
  [page](https://www.mediawiki.org/wiki/Manual:Page_table),
  [pagelinks](https://www.mediawiki.org/wiki/Manual:Pagelinks_table),
  [linktarget](https://www.mediawiki.org/wiki/Manual:Linktarget_table),
  [redirect](https://www.mediawiki.org/wiki/Manual:Redirect_table),
  [page_props](https://www.mediawiki.org/wiki/Manual:Page_props_table).
- `pagelinks` no longer stores target titles. It references `linktarget` through `pl_target_id`.
- Row counts come from `data/interim/parse_stats.json`. They were confirmed by an independent
  count (`data/interim/independent_row_counts.txt`).

### Known limitations of the source

- **Snapshot.** The link tables reflect the state of 2026-10-01. Links change continuously.
- **Template links are included.** `pagelinks` records every link of the *rendered* page,
  including links produced by templates: navigation boxes, authority control, citation
  templates (ISBN, DOI…), portal banners. These are clickable, so they are legitimate game
  moves. But they inflate the degrees of some pages (see `docs/data_quality.md` §5 and the
  bias check).
- **Link tables are updated asynchronously** by the job queue. A small number of rows can be
  stale: for example, 963 links have a `pl_from_namespace` that disagrees with `page`.
- Links that exist only in images or maps, or that are added by JavaScript gadgets, are absent.
- Dump formatting can change between dates. The 2026 dumps put one row per line (see `decisions.md`).

## Secondary collection: MediaWiki API (bias check)

| | |
|---|---|
| Endpoint | `https://fr.wikipedia.org/w/api.php?action=parse&oldid=<page_latest>&prop=text&disablelimitreport=1&disableeditsection=1&format=json&formatversion=2&maxlag=5` plus one `action=query&meta=siteinfo&siprop=namespaces\|namespacealiases` request |
| Pages | the 100 articles with the highest out-degree, the 50 with the highest in-degree (general pages such as countries) and 50 random non-disambiguation articles (seed 7). That is 200 parse requests, plus 1 siteinfo request. |
| Revision | `oldid` = `page_latest` from the dump: the HTML of the **exact revision** of the dump, rendered with today's templates |
| Procedure | `src/wikigame/bias_check.py`: descriptive User-Agent; one request every 3 s, because 1 request/s got HTTP 429 after 14 pages; `Retry-After` and exponential backoff on 429/5xx; `maxlag=5`. Every response is cached in `data/raw/api/` and re-runs make no request. |
| Licence | CC BY-SA 4.0 (rendered article content) |
| Limitations | The revision is that of the dump, but **templates are expanded with their current version** (October 2026), so navigation boxes may differ slightly from the dump's rendering. HTML class names of navigation boxes are a frwiki convention (`.navbox`, `.navbox-container`, `.bandeau-portail`, `#bandeau-portail`, `.navigation-only`). Elements without these classes are counted as body text. |
