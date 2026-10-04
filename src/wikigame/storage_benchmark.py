"""Phase 3 - compare storage options on OUR queries.

Options benchmarked (each in a fresh subprocess, so memory numbers are not polluted):
  csr     Parquet node table + numpy CSR arrays (.npy) loaded in RAM, BFS in numpy.
  duckdb  one DuckDB database file: a node table and the edge list stored twice,
          sorted by source and by target (DuckDB min/max zone maps then make
          "neighbours of these nodes" a range scan). BFS = one SQL query per level.
Options only discussed, not benchmarked: SQLite, Neo4j / graph databases.

Queries, identical for both options:
  Q1 full forward BFS from a node     Q2 full reverse BFS from a node
  Q3 out/in-degree of 1000 nodes      Q4 1000 title -> id and 1000 id -> title lookups
Measured: build time, load time, query time, resident memory, disk size.
The DuckDB BFS result is checked against the numpy BFS (same distances).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time

import numpy as np
import pandas as pd
import psutil

from .common import get_logger, load_config, path, read_json, write_json
from .graph import UNREACHABLE, Graph, bfs, csr_from_edges, load_csr, save_csr

log = get_logger("storage")
U = int(UNREACHABLE)


def rss_mb() -> float:
    return psutil.Process().memory_info().rss / 2**20


def dir_size(paths) -> int:
    return sum(p.stat().st_size for p in paths)


def workload(cfg):
    """Fixed query workload: BFS sources and lookup keys (seeded)."""
    nodes = pd.read_parquet(path(cfg, "processed") / "graph" / "nodes.parquet",
                            columns=["node_id", "title", "in_degree"])
    rng = np.random.default_rng(cfg["analysis"]["seed"])
    ids = rng.choice(len(nodes), size=1000, replace=False)
    return {"bfs_source": int(nodes.in_degree.idxmax()),
            "ids": ids.tolist(), "titles": nodes.title.iloc[ids].tolist()}


def timeit(f, repeat=1):
    best = float("inf")
    out = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = f()
        best = min(best, time.perf_counter() - t0)
    return best, out


# ---- option A: numpy CSR ---------------------------------------------------------

def cleaned_edges(cfg):
    """The cleaned edge list (src, dst) that both options are built from (not timed)."""
    fwd = load_csr(path(cfg, "processed") / "graph", "fwd", mmap=False)
    src = np.repeat(np.arange(fwd.n, dtype=np.int32), np.diff(fwd.indptr))
    return src, np.asarray(fwd.indices), fwd.n


def build_csr(cfg) -> float:
    """Time the CSR build (forward + reverse + .npy files) from the cleaned edges."""
    src, dst, n = cleaned_edges(cfg)
    bdir = path(cfg, "processed") / "bench" / "csr"
    t0 = time.perf_counter()
    save_csr(csr_from_edges(src, dst, n), bdir, "fwd")
    save_csr(csr_from_edges(dst, src, n), bdir, "rev")
    dt = time.perf_counter() - t0
    shutil.rmtree(bdir)  # identical to data/processed/graph, which the queries use
    return dt


def run_csr(cfg, W) -> dict:
    gdir = path(cfg, "processed") / "graph"
    files = list(gdir.glob("*.npy")) + [gdir / "nodes.parquet"]
    r = {"build_s": build_csr(cfg)}
    r.update({"disk_bytes": dir_size(files), "rss_before_load_mb": rss_mb()})
    t0 = time.perf_counter()
    g = Graph.load(gdir, mmap=False)          # everything in RAM
    title_to_id = dict(zip(g.nodes.title, g.nodes.index))
    r["load_s"] = time.perf_counter() - t0
    r["rss_after_load_mb"] = rss_mb()
    src = W["bfs_source"]
    r["q1_fwd_bfs_s"], d1 = timeit(lambda: bfs(g.fwd, src), 3)
    r["q2_rev_bfs_s"], d2 = timeit(lambda: bfs(g.rev, src), 3)
    ids = np.array(W["ids"])
    r["q3_degrees_s"], _ = timeit(lambda: (g.nodes.out_degree.to_numpy()[ids],
                                           g.nodes.in_degree.to_numpy()[ids]), 5)
    titles = g.nodes.title.to_numpy()
    r["q4_lookups_s"], _ = timeit(lambda: ([title_to_id[t] for t in W["titles"]],
                                           [titles[i] for i in ids]), 5)
    r["rss_peak_mb"] = rss_mb()
    r["reached_fwd"] = int((d1 < U).sum())
    r["reached_rev"] = int((d2 < U).sum())
    np.save(path(cfg, "interim") / "bench_ref_fwd.npy", d1)
    return r


# ---- option B: DuckDB -------------------------------------------------------------

def build_duckdb(cfg, db_path) -> float:
    """Time the DuckDB build (edge tables sorted both ways + node table + index)."""
    import duckdb
    gdir = path(cfg, "processed") / "graph"
    src, dst, _ = cleaned_edges(cfg)
    edges = pd.DataFrame({"src": src, "dst": dst})
    del src, dst
    t0 = time.perf_counter()
    con = duckdb.connect(str(db_path))
    con.execute("SET preserve_insertion_order=true")
    con.register("e", edges)
    con.execute("CREATE TABLE edges_by_src AS SELECT src, dst FROM e ORDER BY src, dst")
    con.execute("CREATE TABLE edges_by_dst AS SELECT dst, src FROM e ORDER BY dst, src")
    con.unregister("e")
    del edges
    nodes_p = (gdir / "nodes.parquet").as_posix()
    con.execute(f"CREATE TABLE nodes AS SELECT node_id, page_id, title, is_disambig, "
                f"out_degree, in_degree FROM read_parquet('{nodes_p}') ORDER BY node_id")
    con.execute("CREATE UNIQUE INDEX nodes_title ON nodes(title)")
    con.execute("CHECKPOINT")
    con.close()
    return time.perf_counter() - t0


def duckdb_bfs(con, source: int, table: str, a: str, b: str) -> dict[int, int]:
    """Level-synchronous BFS in SQL: one INSERT ... SELECT per level."""
    con.execute("CREATE OR REPLACE TEMP TABLE dist(node INTEGER, d INTEGER)")
    con.execute("INSERT INTO dist VALUES (?, 0)", [source])
    level = 0
    while True:
        level += 1
        con.execute(f"""
            INSERT INTO dist
            SELECT DISTINCT e.{b}, {level} FROM {table} e
            JOIN (SELECT node FROM dist WHERE d = {level - 1}) f ON e.{a} = f.node
            WHERE NOT EXISTS (SELECT 1 FROM dist v WHERE v.node = e.{b})""")
        added = con.execute(f"SELECT count(*) FROM dist WHERE d = {level}").fetchone()[0]
        if added == 0:
            break
    return dict(con.execute("SELECT node, d FROM dist").fetchall())


def run_duckdb(cfg, W) -> dict:
    import duckdb
    bdir = path(cfg, "processed") / "bench"
    bdir.mkdir(exist_ok=True)
    db = bdir / "graph.duckdb"
    if db.exists():
        db.unlink()  # always rebuild, so the build time is measured on every run
    r = {"build_s": build_duckdb(cfg, db)}
    r["disk_bytes"] = db.stat().st_size
    r["rss_before_load_mb"] = rss_mb()
    t0 = time.perf_counter()
    con = duckdb.connect(str(db), read_only=True)
    con.execute("SET memory_limit='8GB'")
    con.execute("SELECT count(*) FROM nodes").fetchone()
    r["load_s"] = time.perf_counter() - t0
    r["rss_after_load_mb"] = rss_mb()
    src = W["bfs_source"]
    # read-only DB: temp tables live in memory, which is allowed
    r["q1_fwd_bfs_s"], d1 = timeit(lambda: duckdb_bfs(con, src, "edges_by_src", "src", "dst"))
    r["q2_rev_bfs_s"], d2 = timeit(lambda: duckdb_bfs(con, src, "edges_by_dst", "dst", "src"))
    ids = W["ids"]
    r["q3_degrees_s"], _ = timeit(lambda: con.execute(
        "SELECT node_id, out_degree, in_degree FROM nodes WHERE node_id IN (SELECT unnest(?))",
        [ids]).fetchall(), 5)

    def lookups():
        a = [con.execute("SELECT node_id FROM nodes WHERE title = ?", [t]).fetchone()[0] for t in W["titles"]]
        b = [con.execute("SELECT title FROM nodes WHERE node_id = ?", [i]).fetchone()[0] for i in ids]
        return a, b
    r["q4_lookups_s"], _ = timeit(lookups, 1)
    r["rss_peak_mb"] = rss_mb()
    r["reached_fwd"] = len(d1)
    r["reached_rev"] = len(d2)
    ref = np.load(path(cfg, "interim") / "bench_ref_fwd.npy")
    nodes_arr = np.fromiter(d1.keys(), dtype=np.int64)
    dist_arr = np.fromiter(d1.values(), dtype=np.int64)
    r["bfs_matches_numpy"] = bool(len(d1) == int((ref < U).sum())
                                  and np.array_equal(ref[nodes_arr].astype(np.int64), dist_arr))
    con.close()
    return r


def main() -> None:
    cfg = load_config()
    W = workload(cfg)
    results = {"workload": {"bfs_source": W["bfs_source"], "n_lookups": len(W["ids"])}}
    for opt in ["csr", "duckdb"]:  # csr first: it writes the reference BFS
        log.info("benchmarking %s in a fresh process", opt)
        p = subprocess.run([sys.executable, "-m", "wikigame.storage_benchmark", "--option", opt],
                           capture_output=True, text=True)
        if p.returncode:
            log.error(p.stderr)
            raise SystemExit(f"benchmark {opt} failed")
        results[opt] = json.loads(p.stdout.strip().splitlines()[-1])
        log.info("%s: %s", opt, results[opt])
    write_json(results, path(cfg, "generated") / "storage_benchmark.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--option", choices=["csr", "duckdb"])
    a = ap.parse_args()
    if a.option:
        cfg = load_config()
        res = run_csr(cfg, workload(cfg)) if a.option == "csr" else run_duckdb(cfg, workload(cfg))
        print(json.dumps(res))
    else:
        main()
