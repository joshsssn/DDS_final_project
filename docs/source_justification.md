# Why this source (one page)

**Question.** Are the pages that Wikipedia-game players use as landmarks (countries, general
pages, link-rich pages) the best hubs for connecting any two articles? If not, which pages are?

**Why the Wikimedia database dumps.** The question is about the *structure* of the link graph:
which page lies on short paths between arbitrary articles. Answering it requires the complete
set of links between articles, not a sample.
- The dumps are the only public source that provides this set in one consistent snapshot.
  The `pagelinks`, `linktarget`, `page`, `redirect` and `page_props` tables give every internal
  link, the namespace and redirect status of every page, and the disambiguation flag.
- They are official (published by the Wikimedia Foundation), dated, checksummed, freely licensed
  (CC BY-SA / GFDL) and contain no personal data. The pipeline is therefore reproducible: anyone
  can download the same files and verify the same SHA-1 hashes.
- The alternative, crawling pages through the API, would take weeks for ~2.8 M articles. It would
  also mix revisions from different dates and put unnecessary load on Wikimedia servers.

**Why the full graph rather than a sample.** Shortest-path distances are a global property.
Removing nodes or edges from the graph changes distances and can disconnect pairs, so any subgraph
would bias the quantity we measure. The full graph fits in memory on our machine: a CSR array
structure of a few GB. We therefore sample *pairs of articles* (random start/target), not the graph.

**Why French Wikipedia.** It is the edition we play in, so the "intuitive landmarks"
under study are the ones we perceive. It is large (≈ 2.8 M articles) but small enough to process on a
laptop, unlike English Wikipedia (~7 M articles). A single language keeps the scope focused. A
comparison across languages would be a different question.

**What is excluded and why.**
- *Other namespaces* (categories, templates, portals, files, user pages…). In the game, moves
  follow links between articles only.
- *Redirect pages as nodes.* A click on a redirect lands on its target, so redirects are
  resolved instead of being kept as separate nodes.
- *Red links* (links to pages that do not exist). They cannot be clicked through to an article.
- *Disambiguation pages as start/target.* A game between ambiguous titles is not meaningful.
  They remain traversable, since a player can click through them.
- *Human game logs, categories, other datasets and other languages.* Our question is about
  the structural optimum, compared with a structural proxy of perceived landmarks (out-degree)
  and a type label. Logs of real games would answer a different question: what players actually do.

**One small secondary collection.** For 200 pages (100 highest out-degree, 50 highest in-degree, 50 random), the rendered HTML of the exact dump
revision is fetched through the MediaWiki API. This measures how much navigation templates inflate
the out-degree of general pages (see `bias_check.py`). The responses are cached in `data/raw/api/`.

**Known limitations.**
- The dump is a snapshot dated 2026-10-01.
- `pagelinks` records links from the rendered page, *including* links produced by templates
  (navigation boxes, infoboxes, portal banners) that a reader may not perceive as part of the text.
- Links inside images or maps, and links added by user scripts, are not modelled.
- The link graph is directed and unweighted: every click counts the same.
