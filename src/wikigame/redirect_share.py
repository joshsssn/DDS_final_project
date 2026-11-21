"""How much of the candidates' in-degree exists only because redirects are resolved?

Cleaning re-points every link to a redirect onto its final article (docs/data_quality.md §2).
Pages with many alternative names (countries, cities) may gain in-degree from this step, so
the hub ranking could partly depend on it. For every candidate hub of the official sample,
this script splits the in-degree of the cleaned graph into:
  * sources that link to the hub directly (at least one link to its own title);
  * sources that reach it ONLY through one or more redirects: these edges would not exist
    without redirect resolution.

It replays the joins of clean.py on the interim Parquet tables, restricted to the link
targets that lead to a candidate. Check: direct + redirect-only sources must equal the
in-degree of the cleaned graph for every candidate, or the script stops.

Only the in-degree is recomputed. The detours are not: without redirect resolution the
graph itself would change, and so would every BFS.

Output: reports/generated/redirect_share.{csv,json}.
"""
from __future__ import annotations

import pandas as pd
from scipy.stats import spearmanr

from .clean import connect
from .common import ROOT, get_logger, load_config, path, timed, write_json

log = get_logger("redirect_share")


def main() -> None:
    cfg = load_config()
    A = cfg["analysis"]
    gen = path(cfg, "generated")
    sample = gen / f"sample_seed{A['seed']}_n{A['n_pairs']}"
    hubs = pd.read_csv(sample / "hubs.csv")
    lab_p = ROOT / "labels" / "hub_type_labels.csv"
    if lab_p.exists():
        hubs = hubs.merge(pd.read_csv(lab_p)[["page_id", "type"]], on="page_id", how="left")
    rr_p = path(cfg, "processed") / "redirect_resolution.parquet"

    con = connect(cfg)
    con.register("hubs_df", hubs[["page_id"]])
    con.execute("CREATE TABLE hub AS SELECT page_id FROM hubs_df")
    con.execute(f"CREATE VIEW rr AS SELECT * FROM read_parquet('{rr_p.as_posix()}')")
    T: dict = {}
    with timed(log, "link targets leading to a candidate", T):
        # direct: a link target with the hub's own title; redirect: a link target with the
        # title of a namespace-0 redirect whose chain was resolved to the hub (as in clean.py)
        con.execute("""
            CREATE TABLE tgt AS
            SELECT lt.lt_id, p.page_id AS hub_page, TRUE AS direct
            FROM (SELECT page_id, page_title FROM page
                  WHERE page_namespace = 0 AND NOT page_is_redirect
                    AND page_id IN (SELECT page_id FROM hub)) p
            JOIN (SELECT lt_id, lt_title FROM linktarget WHERE lt_namespace = 0) lt
              ON lt.lt_title = p.page_title
            UNION ALL
            SELECT lt.lt_id, r.final_page_id AS hub_page, FALSE AS direct
            FROM (SELECT page_id, final_page_id FROM rr
                  WHERE status = 'resolved' AND final_page_id IN (SELECT page_id FROM hub)) r
            JOIN (SELECT page_id, page_title FROM page WHERE page_namespace = 0) rp
              ON rp.page_id = r.page_id
            JOIN (SELECT lt_id, lt_title FROM linktarget WHERE lt_namespace = 0) lt
              ON lt.lt_title = rp.page_title""")
    with timed(log, "links to the candidates (scan of pagelinks)", T):
        per_hub = con.execute("""
            WITH src_hub AS (
                SELECT t.hub_page, pl.pl_from AS src, bool_or(t.direct) AS has_direct
                FROM pagelinks pl
                JOIN tgt t ON t.lt_id = pl.pl_target_id
                JOIN (SELECT page_id FROM page WHERE page_namespace = 0 AND NOT page_is_redirect) s
                  ON s.page_id = pl.pl_from
                WHERE pl.pl_from <> t.hub_page          -- self-loops are dropped by clean.py
                GROUP BY 1, 2)
            SELECT hub_page AS page_id, count(*) AS in_degree_check,
                   count(*) FILTER (WHERE NOT has_direct) AS in_via_redirect_only
            FROM src_hub GROUP BY 1""").df()
        n_redirect_titles = con.execute(
            "SELECT hub_page AS page_id, count(*) FILTER (WHERE NOT direct) AS n_redirect_targets "
            "FROM tgt GROUP BY 1").df()

    h = hubs.merge(per_hub, on="page_id", how="left").merge(n_redirect_titles, on="page_id", how="left")
    h[["in_degree_check", "in_via_redirect_only", "n_redirect_targets"]] = (
        h[["in_degree_check", "in_via_redirect_only", "n_redirect_targets"]].fillna(0).astype(int))
    bad = h[h.in_degree_check != h.in_degree]
    if len(bad):
        raise RuntimeError(f"in-degree mismatch with the cleaned graph for {len(bad)} candidates: "
                           f"{bad[['title', 'in_degree', 'in_degree_check']].head().to_dict('records')}")
    log.info("check passed: recomputed in-degree = graph in-degree for all %d candidates", len(h))
    h["in_degree_direct"] = h.in_degree - h.in_via_redirect_only
    h["share_redirect_only"] = h.in_via_redirect_only / h.in_degree.where(h.in_degree > 0)
    cols = ["page_id", "title", "type", "top_out_degree", "top_in_degree", "top_path_frequency",
            "random_baseline", "mean_detour", "rank_mean_detour", "in_degree", "in_degree_direct",
            "in_via_redirect_only", "share_redirect_only", "n_redirect_targets"]
    h = h.sort_values("rank_mean_detour")
    h[[c for c in cols if c in h]].to_csv(gen / "redirect_share.csv", index=False)

    def spear(df, a, b):
        df = df[[a, b]].dropna()
        rho, p = spearmanr(df[a], df[b])
        return {"rho": float(rho), "p_value": float(p), "n": int(len(df))}

    def share_stats(df):
        s = df.share_redirect_only.dropna()
        return {"n": int(len(s)), "median": float(s.median()), "mean": float(s.mean()),
                "max": float(s.max()), "max_title": df.loc[s.idxmax(), "title"]}

    cand = h[~h.random_baseline | h.top_out_degree | h.top_in_degree | h.top_path_frequency]
    noref = cand[cand.type != "reference"] if "type" in cand else cand
    top20 = h.nsmallest(20, "rank_mean_detour")
    k = 20
    top_in_full = set(cand.nlargest(k, "in_degree").page_id)
    top_in_direct = set(cand.nlargest(k, "in_degree_direct").page_id)
    out = {
        "sample": sample.name,
        "n_candidates": int(len(h)),
        "check_in_degree_equal_to_graph": True,
        "share_redirect_only": {
            "top20_best_hubs": share_stats(top20),
            "non_random_candidates": share_stats(cand),
            "random_baseline": share_stats(h[h.random_baseline & ~(h.top_out_degree | h.top_in_degree | h.top_path_frequency)]),
        },
        "spearman_in_degree_vs_mean_detour": {
            "with_redirects": spear(cand, "in_degree", "mean_detour"),
            "direct_only": spear(cand, "in_degree_direct", "mean_detour"),
            "with_redirects_without_reference": spear(noref, "in_degree", "mean_detour"),
            "direct_only_without_reference": spear(noref, "in_degree_direct", "mean_detour"),
        },
        "spearman_in_degree_vs_direct_in_degree_candidates": spear(cand, "in_degree", "in_degree_direct"),
        f"top{k}_in_degree_overlap_with_vs_without_redirects": len(top_in_full & top_in_direct),
        "top20_best_hubs": top20[["title", "type", "mean_detour", "in_degree", "in_degree_direct",
                                  "share_redirect_only"]].to_dict("records"),
        "timings_s": T,
    }
    write_json(out, gen / "redirect_share.json")
    log.info("top-20 hubs: median share of in-degree through redirects only = %.3f",
             out["share_redirect_only"]["top20_best_hubs"]["median"])


if __name__ == "__main__":
    main()
