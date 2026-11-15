# Storage: comparison and choice

All numbers come from `src/wikigame/storage_benchmark.py`, which writes
`reports/generated/storage_benchmark.json`.
- Machine: 32 GB RAM, i7-11370H, Windows 11, SSD.
- Each option runs in a fresh process.
- Both options are built from the same cleaned edge list: 2,782,511 nodes and 235,902,925 edges.

## What we need to store, and which queries we run

The data is a **directed graph** of 2.78 M nodes and 236 M edges, plus a node table (id, page id,
title, disambiguation flag, degrees). It is written once by `clean.py` and then **only read**.

Our actual queries:

| Query | Used for | How often |
|---|---|---|
| Q1 full forward BFS from a node | d(h, ·) for each hub | once per hub (385 hubs in the cache) |
| Q2 full reverse BFS from a node | d(·, h) for each hub | once per hub (385) |
| Q1' bidirectional BFS s → t | d(s, t) for each random pair; uses both the forward and reverse adjacency | 400,000 times (two samples of 200,000) |
| Q3 out-/in-degree lookups | perceived criterion, top-K | thousands |
| Q4 title ↔ id lookups | labels, reports, bias check | thousands |

Q1 and Q2 dominate the cost. A BFS touches every edge, so what matters is **how fast all 236 M
edges can be scanned**, many times. Transactions, concurrent writers and ad-hoc joins are not needed.
In the official sample, 299 candidate hubs are evaluated, each with two BFS; the cache holds 385
hub vectors in total, because it also keeps the hubs of the first 2,000-pair sample, of the
independent seed and of the strongly-connected-component step.

## Options

**A. Parquet node table + NumPy CSR arrays.** The adjacency lists are stored as
`indptr`/`indices` `.npy` files, forward and reverse, and loaded or memory-mapped in RAM. The BFS is
vectorised NumPy (`graph.py`).

**B. DuckDB database file.** One file holds:
- a node table with a unique index on the title;
- the edge list stored twice, sorted by source and by target, so that DuckDB's zone maps turn
  "neighbours of a set of nodes" into range scans.

The BFS is level-synchronous SQL, with one `INSERT … SELECT … WHERE NOT EXISTS` per level. Its
result is checked against the NumPy BFS: `bfs_matches_numpy = true`.

**Discussed, not benchmarked.**
- **SQLite.** It is row-oriented, has no vectorised execution, and a B-tree index on 236 M rows
  takes several GB. Each BFS level is a large join, which SQLite does much more slowly than DuckDB.
  It brings nothing that DuckDB does not do better for this workload.
- **Neo4j or another graph database.** Native graph traversals are the right model in principle.
  But Neo4j needs a server (Java or Docker), an import of 236 M relationships, and a
  disk-and-page-cache layout designed for transactional workloads. It also has no advantage for
  *full* BFS over the whole graph, which is exactly our query. We wanted the simplest, lightest
  setup, with no server and no Docker, so it was excluded from the benchmark.
- **networkx / igraph in memory.** networkx stores Python objects. A back-of-envelope estimate,
  not a measurement, is ~100 bytes per edge, so about 24 GB for 236 M edges, with pure-Python
  traversal speed. igraph would work, but it adds a C library dependency to
  do what a 40-line NumPy BFS already does at the same order of speed.

## Results

| Measure | NumPy CSR + Parquet | DuckDB |
|---|---:|---:|
| Build from the cleaned edge list | 43.6 s | 29.3 s |
| Disk size | 2.03 GB | 1.53 GB |
| Load / connect | 5.5 s (everything in RAM) | 0.02 s (lazy) |
| Peak resident memory (after load: 2.5 GB vs 0.4 GB) | 2.7 GB | 2.4 GB |
| Q1 full forward BFS | **3.4 s** | 9.4 s |
| Q2 full reverse BFS | **3.3 s** | 12.0 s |
| Q3 degrees of 1,000 nodes | **0.06 ms** | 12.4 ms |
| Q4 2,000 title/id lookups | **0.4 ms** | 1,515 ms (one query per lookup) |

Notes:
- Memory: the CSR process holds the whole graph. DuckDB's memory grows during the BFS, in its
  buffer pool and temporary tables.
- BFS reach is the same for both: 2,732,071 nodes forward and 2,782,255 reverse.

## Choice: NumPy CSR arrays + Parquet node table

**Observations.**
- On the query that dominates our workload (full BFS), CSR is **about 2.8× faster forward and
  3.6× faster reverse** than DuckDB.
- Point lookups are about 190× faster for degrees, and about 3,800× faster for title/id lookups.
  That last ratio compares one vectorised NumPy lookup with 2,000 separate SQL statements, so it
  measures the query interface as much as the storage engine; a single `IN (...)` query in DuckDB
  would narrow the gap. It does not affect the conclusion, which rests on the BFS timings.
- DuckDB is smaller on disk, builds faster and opens instantly.
- Peak memory is similar. DuckDB's is a transient peak during the BFS; CSR holds the graph permanently.

**Why CSR.**
1. **Queries.** The 385 hubs in the cache need 770 full BFS runs: about 43 min single-core for
   CSR at the measured speeds, versus about 2.3 h for DuckDB. In practice CSR runs several BFS in
   parallel on memory-mapped arrays shared between processes. The 400,000 pair distances use a
   bidirectional BFS on the same arrays (329 s for 198,000 pairs with 6 processes); it was not
   benchmarked in DuckDB.
2. **Structure.** A static directed graph is exactly what CSR represents: neighbour lists are
   contiguous slices, which is the ideal access pattern for BFS. The relational model would encode
   the same graph as a join problem.
3. **Scale.** 2 GB fits comfortably in RAM on our machines (≥ 16 GB).
4. **Reproducibility and simplicity.** The `.npy` and Parquet formats are open and need only
   `numpy` and `pyarrow`. There is no server and no schema migration, and the files are read
   identically on every OS.

**Where DuckDB is still used.** It remains the right tool for the *relational* part of the pipeline:
the cleaning joins between `pagelinks` (470 M rows), `linktarget`, `page` and `redirect` run
out-of-core and multi-threaded directly on the Parquet files (`clean.py`). We use each tool for the
access pattern it was designed for.

**Limitations of the choice.**
- CSR is static: any change to the graph means re-running the cleaning. That took 6.6 min as measured
  with the old CSR builder (6 min of it building the CSR); the current builder needs 44 s for that part.
- CSR does not support ad-hoc queries. For exploratory questions on the edge list, the Parquet and
  DuckDB route remains available.
- The analysis BFS cache is keyed by node ids. It is protected by a fingerprint of the graph and
  wiped automatically if the graph changes.
