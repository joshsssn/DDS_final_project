"""Phase 2 - measure data quality, then build the cleaned article graph.

Measure first (everything goes to data/processed/quality.json), then apply:
  * keep namespace 0 non-redirect pages as nodes ("articles");
  * resolve redirects on link targets, following chains up to `max_redirect_depth`;
    broken redirects, cycles, redirects leaving namespace 0 and over-long chains are dropped;
  * drop links whose source is not an article (other namespaces, redirect pages),
    links to other namespaces, links to non-existent pages (red links), self-loops
    and duplicate edges created by redirect resolution;
  * flag disambiguation pages (page_props 'disambiguation'): kept as traversable nodes,
    excluded later as start/target of random pairs.
Output: data/processed/graph/ (see graph.py for the layout).

Joins between the large tables (hundreds of millions of links x tens of millions of
link targets) are done with DuckDB directly on the Parquet files (out-of-core, multi-
threaded); redirect chains and the final CSR are done in numpy.
"""
from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd

from .common import get_logger, load_config, path, timed, write_json
from .graph import csr_from_edges, save_csr

log = get_logger("clean")
CHAIN_LIMIT = 50  # chains are *measured* up to this length; longer = cycle


def connect(cfg) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    tmp = path(cfg, "interim") / "duckdb_tmp"
    con.execute(f"SET temp_directory='{tmp.as_posix()}'")
    con.execute("SET memory_limit='18GB'")
    con.execute("SET preserve_insertion_order=false")
    interim = path(cfg, "interim")
    for t in cfg["dump"]["tables"]:
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{(interim / f'{t}.parquet').as_posix()}')")
    return con


def resolve_redirects(con, max_depth: int, Q: dict) -> pd.DataFrame:
    """Return, for every namespace-0 redirect page, its final article (or NULL) and why."""
    rd = con.execute("""
        SELECT p.page_id AS rd_from, r.rd_from IS NULL AS no_row,
               coalesce(r.rd_interwiki, '') <> '' AS interwiki,
               r.rd_namespace, coalesce(r.rd_fragment, '') <> '' AS has_fragment,
               t.page_id AS next_id
        FROM page p
        LEFT JOIN redirect r ON r.rd_from = p.page_id
        -- pure equi-joins only (extra conditions in ON make DuckDB use a nested loop):
        -- a redirect leads to a namespace-0 page only if it is local and in namespace 0
        LEFT JOIN (SELECT rd_from, rd_title FROM redirect
                   WHERE rd_namespace = 0 AND coalesce(rd_interwiki, '') = '') rv
               ON rv.rd_from = p.page_id
        LEFT JOIN (SELECT page_id, page_title FROM page WHERE page_namespace = 0) t
               ON t.page_title = rv.rd_title
        WHERE p.page_namespace = 0 AND p.page_is_redirect
    """).df()
    art = con.execute("SELECT page_id FROM page WHERE page_namespace = 0 AND NOT page_is_redirect").df()
    maxid = int(max(rd.rd_from.max(), art.page_id.max())) + 1
    is_article = np.zeros(maxid, dtype=bool)
    is_article[art.page_id.to_numpy()] = True
    nxt = np.full(maxid, -1, dtype=np.int64)
    valid_next = rd.next_id.notna().to_numpy()
    nxt[rd.rd_from.to_numpy()[valid_next]] = rd.next_id.to_numpy()[valid_next].astype(np.int64)

    # Follow every chain in parallel (vectorised over all redirects).
    start = rd.rd_from.to_numpy()
    cur = nxt[start]
    length = np.ones(len(start), dtype=np.int64)
    active = (cur >= 0) & ~is_article[np.maximum(cur, 0)]
    while active.any() and length[active].min() <= CHAIN_LIMIT:
        idx = np.flatnonzero(active)
        cur[idx] = nxt[cur[idx]]
        length[idx] += 1
        active[idx] = (cur[idx] >= 0) & ~is_article[np.maximum(cur[idx], 0)] & (length[idx] <= CHAIN_LIMIT)
    ends_on_article = (cur >= 0) & is_article[np.maximum(cur, 0)]

    status = np.full(len(start), "broken", dtype=object)
    status[rd.no_row.to_numpy()] = "missing_redirect_row"
    status[rd.interwiki.to_numpy()] = "interwiki"
    other_ns = (~rd.no_row & ~rd.interwiki & (rd.rd_namespace != 0)).to_numpy()
    status[other_ns] = "target_other_namespace"
    status[ends_on_article] = "resolved"
    cyc = ~ends_on_article & (cur >= 0)  # still on a redirect after CHAIN_LIMIT hops
    status[cyc] = "cycle"
    too_deep = ends_on_article & (length > max_depth)
    status[too_deep] = "too_deep"

    out = pd.DataFrame({"page_id": start, "status": status, "chain_length": length,
                        "final_page_id": np.where(status == "resolved", cur, -1)})
    Q["redirects"] = {
        "ns0_redirect_pages": int(len(out)),
        "status_counts": out.status.value_counts().to_dict(),
        "chain_length_distribution_resolvable": (
            pd.Series(length[ends_on_article]).value_counts().sort_index().to_dict()),
        "double_redirects_(chain_length>=2)": int((ends_on_article & (length >= 2)).sum()),
        "with_section_fragment": int(rd.has_fragment.sum()),
        "max_depth_policy": max_depth,
    }
    return out


def main() -> None:
    cfg = load_config()
    max_depth = int(cfg["clean"]["max_redirect_depth"])
    proc = path(cfg, "processed")
    gdir = proc / "graph"
    gdir.mkdir(parents=True, exist_ok=True)
    con = connect(cfg)
    Q: dict = {"timings_s": {}}
    T = Q["timings_s"]

    # ---- 1. pages ---------------------------------------------------------------
    with timed(log, "pages", T):
        Q["pages_per_namespace"] = [
            {"namespace": int(a), "pages": int(b), "redirects": int(c)} for a, b, c in con.execute(
                "SELECT page_namespace, count(*), sum(page_is_redirect::INT) FROM page "
                "GROUP BY 1 ORDER BY 2 DESC").fetchall()]
        Q["pages_total"] = sum(r["pages"] for r in Q["pages_per_namespace"])
        con.execute("""
            CREATE TABLE nodes AS
            SELECT (row_number() OVER (ORDER BY page_id) - 1)::INTEGER AS node_id,
                   page_id, page_title AS title, page_latest
            FROM page WHERE page_namespace = 0 AND NOT page_is_redirect""")
        dis = con.execute("""
            SELECT count(*) AS flagged,
                   count(*) FILTER (WHERE n.page_id IS NOT NULL) AS articles,
                   count(*) FILTER (WHERE p.page_namespace = 0 AND p.page_is_redirect) AS ns0_redirects,
                   count(*) FILTER (WHERE p.page_id IS NULL) AS missing_page
            FROM (SELECT DISTINCT pp_page FROM page_props) d
            LEFT JOIN page p ON p.page_id = d.pp_page
            LEFT JOIN nodes n ON n.page_id = d.pp_page""").fetchone()
        Q["disambiguation"] = dict(zip(["pages_flagged", "flagged_articles",
                                        "flagged_ns0_redirects", "flagged_missing_page"],
                                       map(int, dis)))
        Q["articles"] = int(con.execute("SELECT count(*) FROM nodes").fetchone()[0])

    # ---- 2. redirects -----------------------------------------------------------
    with timed(log, "redirect resolution", T):
        res = resolve_redirects(con, max_depth, Q)
        con.register("res_df", res)
        con.execute("CREATE TABLE redirect_resolution AS SELECT * FROM res_df")
        con.unregister("res_df")
        res.to_parquet(proc / "redirect_resolution.parquet", index=False)

    # ---- 3. classify link targets -----------------------------------------------
    with timed(log, "classify link targets", T):
        con.execute("""
            CREATE TABLE lt_class AS
            SELECT lt.lt_id,
                   CASE WHEN lt.lt_namespace <> 0 THEN 'other_namespace'
                        WHEN p.page_id IS NULL THEN 'red_link'
                        WHEN NOT p.page_is_redirect THEN 'article'
                        ELSE 'redirect_' || rr.status END AS target_class,
                   coalesce(na.node_id, nr.node_id) AS node_id
            FROM linktarget lt
            LEFT JOIN (SELECT page_id, page_namespace, page_title, page_is_redirect
                       FROM page WHERE page_namespace = 0) p
                   ON p.page_namespace = lt.lt_namespace AND p.page_title = lt.lt_title
            LEFT JOIN nodes na ON na.page_id = p.page_id               -- nodes = articles only
            LEFT JOIN redirect_resolution rr ON rr.page_id = p.page_id -- redirects only
            LEFT JOIN nodes nr ON nr.page_id = rr.final_page_id""")
        Q["linktarget"] = {
            "rows": int(con.execute("SELECT count(*) FROM linktarget").fetchone()[0]),
            "by_class": {k: int(v) for k, v in con.execute(
                "SELECT target_class, count(*) FROM lt_class GROUP BY 1 ORDER BY 2 DESC").fetchall()},
        }

    # ---- 4. classify links -------------------------------------------------------
    with timed(log, "classify links", T):
        rows = con.execute("""
            SELECT CASE WHEN s.page_id IS NULL THEN 'missing_source_page'
                        WHEN s.page_namespace <> 0 THEN 'source_other_namespace'
                        WHEN s.page_is_redirect THEN 'source_is_redirect'
                        ELSE 'source_article' END AS source_class,
                   coalesce(t.target_class, 'missing_linktarget') AS target_class,
                   count(*) AS n,
                   count(*) FILTER (WHERE s.page_id IS NOT NULL
                                    AND s.page_namespace <> pl.pl_from_namespace) AS ns_mismatch
            FROM pagelinks pl
            LEFT JOIN page s ON s.page_id = pl.pl_from
            LEFT JOIN lt_class t ON t.lt_id = pl.pl_target_id
            GROUP BY 1, 2 ORDER BY 3 DESC""").fetchall()
        Q["links"] = {
            "rows": sum(int(r[2]) for r in rows),
            "by_source_and_target_class": [
                {"source": a, "target": b, "n": int(c)} for a, b, c, _ in rows],
            "pl_from_namespace_inconsistent_with_page": sum(int(r[3]) for r in rows),
        }

    # ---- 5. extract article -> article edges ------------------------------------
    with timed(log, "extract edges", T):
        edges = con.execute("""
            SELECT s.node_id AS src, t.node_id AS dst
            FROM pagelinks pl
            JOIN nodes s ON s.page_id = pl.pl_from
            JOIN lt_class t ON t.lt_id = pl.pl_target_id
            WHERE t.node_id IS NOT NULL""").fetchnumpy()
        src = edges["src"].astype(np.int32)
        dst = edges["dst"].astype(np.int32)
        del edges
    n = Q["articles"]
    with timed(log, "self-loops, duplicates, CSR", T):
        E = {"candidate_edges": int(src.size)}
        keep = src != dst
        E["self_loops"] = int((~keep).sum())
        key = np.unique((src[keep].astype(np.int64) << 32) | dst[keep].astype(np.int64))
        del src, dst, keep
        E["duplicates_after_redirect_resolution"] = E["candidate_edges"] - E["self_loops"] - int(key.size)
        E["final_edges"] = int(key.size)
        s = (key >> 32).astype(np.int32)
        d = (key & 0xFFFFFFFF).astype(np.int32)
        del key
        fwd = csr_from_edges(s, d, n)
        rev = csr_from_edges(d, s, n)
        del s, d
        save_csr(fwd, gdir, "fwd")
        save_csr(rev, gdir, "rev")
        Q["edges"] = E

    # ---- 6. nodes table, degrees --------------------------------------------------
    with timed(log, "nodes table", T):
        nodes = con.execute("""
            SELECT n.node_id, n.page_id, n.title, n.page_latest,
                   (n.page_id IN (SELECT pp_page FROM page_props)) AS is_disambig
            FROM nodes n ORDER BY n.node_id""").df()
        nodes["out_degree"] = fwd.degree().astype(np.int32)
        nodes["in_degree"] = rev.degree().astype(np.int32)
        nodes.to_parquet(gdir / "nodes.parquet", index=False)
        out0 = nodes.out_degree == 0
        in0 = nodes.in_degree == 0
        Q["degrees"] = {
            "nodes": n,
            "sinks_out_degree_0": int(out0.sum()),
            "orphans_in_degree_0": int(in0.sum()),
            "isolated_both_0": int((out0 & in0).sum()),
            "disambiguation_nodes": int(nodes.is_disambig.sum()),
            "mean_out_degree": float(nodes.out_degree.mean()),
            "median_out_degree": float(nodes.out_degree.median()),
            "max_out_degree": int(nodes.out_degree.max()),
            "median_in_degree": float(nodes.in_degree.median()),
            "max_in_degree": int(nodes.in_degree.max()),
        }
        write_degree_counts(nodes, path(cfg, "generated") / "degree_counts.csv")
    write_json(Q, proc / "quality.json")
    log.info("graph: %d nodes, %d edges", n, Q["edges"]["final_edges"])


def degree_counts(nodes: pd.DataFrame) -> pd.DataFrame:
    """Number of articles per degree value (columns: degree, n_out, n_in).

    A few thousand rows instead of the 95 MB node table: this small file is versioned,
    so the degree figure can be redrawn from a fresh copy of the repository without
    rebuilding the graph from the dumps."""
    out = nodes.out_degree.value_counts().rename("n_out")
    inn = nodes.in_degree.value_counts().rename("n_in")
    c = pd.concat([out, inn], axis=1).fillna(0).astype(np.int64).sort_index()
    c.index.name = "degree"
    return c.reset_index()


def write_degree_counts(nodes: pd.DataFrame, dest) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    degree_counts(nodes).to_csv(dest, index=False)
    log.info("wrote %s", dest)


if __name__ == "__main__":
    main()
