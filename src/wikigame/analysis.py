"""Phase 4 - descriptive statistics, candidate hubs, hub quality, perceived vs real.

Persistent cache (data/processed/cache/), so that new samples never redo old work:
  pairs.parquet        every (s, t) pair ever evaluated: d(s, t) and one sampled
                       shortest path. Keyed by (s, t), independent of the sample.
  samples.parquet      which pairs belong to which sample (seed); a new seed draws
                       pairs that were never used by a previous sample.
  hubs/{node}.npz      full forward and reverse BFS distance vectors of a hub.

Sample definition: with seed `seed`, s and t are drawn independently and uniformly
among non-disambiguation articles (s != t), pairs already used by another sample are
skipped, and the first `n_pairs` draws form the sample. Increasing `n_pairs` with the
same seed extends the sample (prefix property), so only new pairs are computed.

Usage: python -m wikigame.analysis [--seed S] [--n-pairs N]
"""
from __future__ import annotations

import argparse
import os
import shutil
from multiprocessing import Pool

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from .common import (ROOT, SMALL_TYPES, get_logger, group_small_types, load_config, path,
                     read_json, timed, write_json)
from .graph import UNREACHABLE, Graph, bfs, bidirectional_distance, graph_fingerprint

log = get_logger("analysis")
U = int(UNREACHABLE)

# ---- worker processes (graph memory-mapped once per process) --------------------
_G: dict = {}


def _init(gdir):
    _G["g"] = Graph.load(gdir, mmap=True, with_titles=False)


def _pair_task(args):
    pair_key, s, t, seed = args
    g = _G["g"]
    # bidirectional BFS: same exact distance as a one-sided BFS, ~300x fewer edges scanned
    rng = np.random.default_rng([seed, pair_key])
    d, p = bidirectional_distance(g.fwd, g.rev, s, t, rng)
    return s, t, d, p


def _hub_task(args):
    h, out = args
    g = _G["g"]
    fwd = bfs(g.fwd, h)
    rev = bfs(g.rev, h)
    tmp = out[:-len(".npz")] + ".tmp.npz"
    np.savez(tmp, fwd=fwd, rev=rev)
    os.replace(tmp, out)  # atomic: an interrupted run never leaves a truncated cache file
    return h


# ---- cache validity ----------------------------------------------------------------

def check_cache(cfg, gdir) -> None:
    """The cache stores node ids and distances: it is only valid for the exact graph
    it was computed on. Compare the graph fingerprint with the one stored in the cache
    and wipe the cache on mismatch (or if the cache has no fingerprint)."""
    cache = path(cfg, "cache")
    fp = graph_fingerprint(gdir)
    fp["max_redirect_depth"] = cfg["clean"]["max_redirect_depth"]
    fp["dump_date"] = cfg["dump"]["date"]
    fp_p = cache / "fingerprint.json"
    if fp_p.exists() and read_json(fp_p) == fp:
        return
    if any(cache.iterdir()):
        log.warning("cache does not match the current graph: wiping %s", cache)
        shutil.rmtree(cache)
        cache.mkdir(parents=True)
    write_json(fp, fp_p)


# ---- sampling ------------------------------------------------------------------

def draw_sample(eligible: np.ndarray, seed: int, n_pairs: int, used_elsewhere: set) -> pd.DataFrame:
    """Deterministic stream of pairs for `seed`; the first n_pairs valid ones."""
    rng = np.random.default_rng(seed)
    out, seen = [], set()
    while len(out) < n_pairs:
        batch = eligible[rng.integers(0, len(eligible), size=(4 * n_pairs, 2))]
        for s, t in batch:
            key = (int(s), int(t))
            if s == t or key in seen or key in used_elsewhere:
                continue
            seen.add(key)
            out.append(key)
            if len(out) == n_pairs:
                break
    return pd.DataFrame(out, columns=["s", "t"])


def get_sample(cfg, g: Graph, seed: int, n_pairs: int) -> pd.DataFrame:
    cache = path(cfg, "cache")
    reg_p = cache / "samples.parquet"
    reg = pd.read_parquet(reg_p) if reg_p.exists() else pd.DataFrame(
        {"seed": pd.Series(dtype="int64"), "idx": pd.Series(dtype="int64"),
         "s": pd.Series(dtype="int32"), "t": pd.Series(dtype="int32")})
    other = reg[reg.seed != seed]
    used = set(zip(other.s.tolist(), other.t.tolist()))
    eligible = np.flatnonzero(~g.nodes["is_disambig"].to_numpy())
    sample = draw_sample(eligible, seed, n_pairs, used)
    sample.insert(0, "idx", np.arange(len(sample)))
    sample.insert(0, "seed", seed)
    reg = pd.concat([other, sample], ignore_index=True)
    reg.astype({"s": "int32", "t": "int32"}).to_parquet(reg_p, index=False)
    return sample


def evaluate_pairs(cfg, gdir, sample: pd.DataFrame, seed: int) -> pd.DataFrame:
    """d(s, t) and a sampled shortest path for every pair, using/extending the cache."""
    cache_p = path(cfg, "cache") / "pairs.parquet"
    cached = pd.read_parquet(cache_p) if cache_p.exists() else pd.DataFrame(
        columns=["s", "t", "d", "path"])
    have = set(zip(cached.s.tolist(), cached.t.tolist()))
    todo = [(int(i), int(s), int(t), seed) for i, s, t in sample[["idx", "s", "t"]].itertuples(index=False)
            if (s, t) not in have]
    log.info("pairs: %d in sample, %d cached, %d to compute", len(sample), len(sample) - len(todo), len(todo))
    if todo:
        rows = []

        def save():
            # checkpoint: an interrupted run keeps everything computed so far
            new = pd.DataFrame(rows, columns=["s", "t", "d", "path"])
            both = pd.concat([cached, new], ignore_index=True) if len(cached) else new
            tmp = cache_p.with_suffix(".tmp.parquet")
            both.to_parquet(tmp, index=False)
            os.replace(tmp, cache_p)
            return both

        with Pool(cfg["analysis"]["bfs_workers"], _init, (gdir,)) as pool:
            for k, r in enumerate(pool.imap_unordered(_pair_task, todo, chunksize=64), 1):
                rows.append(r)
                if k % 20000 == 0:
                    log.info("pairs: %d / %d", k, len(todo))
                    save()
        cached = save()
    return sample.merge(cached, on=["s", "t"], how="left")


def ensure_hubs(cfg, gdir, hubs) -> None:
    hdir = path(cfg, "cache") / "hubs"
    hdir.mkdir(exist_ok=True)
    todo = [(int(h), str(hdir / f"{int(h)}.npz")) for h in hubs if not (hdir / f"{int(h)}.npz").exists()]
    log.info("hubs: %d requested, %d to compute (2 BFS each)", len(hubs), len(todo))
    if todo:
        with Pool(cfg["analysis"]["bfs_workers"], _init, (gdir,)) as pool:
            for k, _ in enumerate(pool.imap_unordered(_hub_task, todo), 1):
                if k % 20 == 0:
                    log.info("hubs: %d / %d", k, len(todo))


def load_hub(cfg, h):
    z = np.load(path(cfg, "cache") / "hubs" / f"{int(h)}.npz")
    return z["fwd"], z["rev"]


# ---- metrics ---------------------------------------------------------------------

def hub_metrics(cfg, h: int, pairs: pd.DataFrame) -> dict:
    """Mean detour and on-shortest-path rate of hub h over the reachable pairs."""
    fwd, rev = load_hub(cfg, h)
    p = pairs[(pairs.d < U) & (pairs.s != h) & (pairs.t != h)]
    s, t, d = p.s.to_numpy(), p.t.to_numpy(), p.d.to_numpy().astype(np.int64)
    dsh = rev[s].astype(np.int64)   # rev BFS from h gives d(x, h)
    dht = fwd[t].astype(np.int64)   # fwd BFS from h gives d(h, x)
    ok = (dsh < U) & (dht < U)
    detour = dsh[ok] + dht[ok] - d[ok]
    assert (detour >= 0).all(), "triangle inequality violated: bug"
    n = len(p)
    # Populations: mean/median detour are over the pairs where h is connected (s reaches h
    # and h reaches t); on_path_rate is over ALL evaluated pairs (a non-connected pair
    # counts as "not on a shortest path"). `coverage` = share of connected pairs; rankings
    # by mean detour only consider hubs with coverage >= analysis.min_coverage.
    return {
        "node_id": h,
        "pairs_evaluated": n,
        "pairs_connected": int(ok.sum()),
        "pairs_h_not_connected": int((~ok).sum()),   # s cannot reach h, or h cannot reach t
        "pairs_s_cannot_reach_h": int((dsh >= U).sum()),   # h unreachable from the start
        "pairs_h_cannot_reach_t": int((dht >= U).sum()),   # h does not reach the target
        "coverage": float(ok.mean()) if n else np.nan,
        "mean_detour": float(detour.mean()) if ok.any() else np.nan,
        "median_detour": float(np.median(detour)) if ok.any() else np.nan,
        "on_path_rate": float((detour == 0).sum() / n) if n else np.nan,
        "reach_fwd": int((fwd < U).sum()),
        "reach_rev": int((rev < U).sum()),
        "mean_dist_from_h": float(fwd[fwd < U].mean()),
        "mean_dist_to_h": float(rev[rev < U].mean()),
    }


def write_pairs_csv(pairs: pd.DataFrame, nodes: pd.DataFrame, dest) -> None:
    """Export of the evaluated pairs, readable without the (unversioned) node table.

    s, t are internal node ids (row numbers of nodes.parquet, used by the cache);
    s_page_id, t_page_id are Wikipedia page ids (stable, usable with the MediaWiki API).
    d is the distance in clicks, left EMPTY when t is unreachable from s: the 255
    sentinel of the uint8 cache must never leak into a mean."""
    page = nodes.page_id.to_numpy()
    df = pd.DataFrame({"idx": pairs.idx, "s": pairs.s, "t": pairs.t,
                       "s_page_id": page[pairs.s], "t_page_id": page[pairs.t],
                       "d": pd.array(pairs.d, dtype="Int64")})
    df.loc[pairs.d.to_numpy() >= U, "d"] = pd.NA
    df.to_csv(dest, index=False)


def main(seed: int | None = None, n_pairs: int | None = None) -> None:
    cfg = load_config()
    A = cfg["analysis"]
    seed = A["seed"] if seed is None else seed
    n_pairs = A["n_pairs"] if n_pairs is None else n_pairs
    K, R = A["top_k"], A["n_random_baseline"]
    gdir = path(cfg, "processed") / "graph"
    out = path(cfg, "generated") / f"sample_seed{seed}_n{n_pairs}"
    out.mkdir(parents=True, exist_ok=True)
    check_cache(cfg, gdir)
    g = Graph.load(gdir, mmap=True)
    nodes = g.nodes
    res: dict = {"seed": seed, "n_pairs": n_pairs, "top_k": K, "timings_s": {}}
    T = res["timings_s"]

    # ---- descriptive: degrees ---------------------------------------------------
    n, m = g.fwd.n, int(g.fwd.indptr[-1])
    od, idg = nodes.out_degree.to_numpy(), nodes.in_degree.to_numpy()
    res["graph"] = {"nodes": n, "edges": m,
                    "out_degree_quantiles": {q: float(np.quantile(od, q)) for q in (0.25, 0.5, 0.75, 0.9, 0.99)},
                    "in_degree_quantiles": {q: float(np.quantile(idg, q)) for q in (0.25, 0.5, 0.75, 0.9, 0.99)},
                    "disambiguation_nodes": int(nodes.is_disambig.sum())}

    # ---- largest strongly connected component ----------------------------------
    # The SCC containing the most-linked article = fwd-reach ∩ rev-reach of that node.
    # If it holds more than half of the nodes it is necessarily the largest SCC.
    root = int(np.argmax(idg))
    with timed(log, "largest SCC", T):
        ensure_hubs(cfg, gdir, [root])
        f, r = load_hub(cfg, root)
        lscc = (f < U) & (r < U)
    np.save(path(cfg, "processed") / "largest_scc_mask.npy", lscc)
    res["largest_scc"] = {"root_node": root, "root_title": nodes.title.iat[root],
                          "size": int(lscc.sum()), "share_of_nodes": float(lscc.mean()),
                          "is_largest_proven": bool(lscc.sum() > n / 2),
                          "reachable_from_root": int((f < U).sum()),
                          "reaching_root": int((r < U).sum())}

    # ---- random pairs ------------------------------------------------------------
    with timed(log, "pairs", T):
        sample = get_sample(cfg, g, seed, n_pairs)
        pairs = evaluate_pairs(cfg, gdir, sample, seed)
    write_pairs_csv(pairs, nodes, out / "pairs.csv")
    reach = pairs.d < U
    res["pairs"] = {
        "n": len(pairs), "reachable": int(reach.sum()), "unreachable": int((~reach).sum()),
        "unreachable_share": float((~reach).mean()),
        "distance_distribution": {int(k): int(v) for k, v in pairs.d[reach].value_counts().sort_index().items()},
        "mean_distance": float(pairs.d[reach].mean()), "median_distance": float(pairs.d[reach].median()),
        "both_in_largest_scc": int((lscc[pairs.s] & lscc[pairs.t]).sum()),
        "unreachable_breakdown": {
            "s_outside_lscc": int((~reach & ~lscc[pairs.s]).sum()),
            "t_outside_lscc": int((~reach & ~lscc[pairs.t]).sum()),
            "s_is_sink": int((~reach & (od[pairs.s] == 0)).sum()),
            "t_is_orphan": int((~reach & (idg[pairs.t] == 0)).sum()),
        },
    }

    # ---- candidate hubs ------------------------------------------------------------
    inner = [v for p in pairs.path[reach] for v in list(p)[1:-1]]  # intermediate nodes only
    path_freq = pd.Series(inner, dtype="int64").value_counts()
    # deterministic order: frequency desc, then node id asc (ties are frequent)
    path_freq = path_freq.rename_axis("node").reset_index(name="n").sort_values(
        ["n", "node"], ascending=[False, True]).set_index("node")["n"]
    top_out = list(np.argsort(-od, kind="stable")[:K])
    top_in = list(np.argsort(-idg, kind="stable")[:K])
    top_path = list(path_freq.index[:K])
    rng = np.random.default_rng([seed, 999])
    eligible = np.flatnonzero(~nodes.is_disambig.to_numpy())
    random_base = list(rng.choice(eligible, size=R, replace=False))
    cand = pd.DataFrame({"node_id": sorted(set(map(int, top_out + top_in + top_path + random_base)))})
    cand["top_out_degree"] = cand.node_id.isin(top_out)
    cand["top_in_degree"] = cand.node_id.isin(top_in)
    cand["top_path_frequency"] = cand.node_id.isin(top_path)
    cand["random_baseline"] = cand.node_id.isin(random_base)
    kth = int(path_freq.iloc[K - 1]) if len(path_freq) >= K else 0
    res["candidates"] = {"union_size": len(cand), "path_frequency_of_kth_candidate": kth,
                         "path_frequency_ties_at_kth": int((path_freq == kth).sum()), "top_out": K, "top_in": K, "top_path": len(top_path),
                         "random_baseline": R,
                         "overlap_out_in": len(set(top_out) & set(top_in)),
                         "overlap_out_path": len(set(top_out) & set(top_path)),
                         "overlap_in_path": len(set(top_in) & set(top_path)),
                         "distinct_intermediate_nodes_on_sampled_paths": int(len(path_freq))}

    # ---- hub quality ------------------------------------------------------------------
    with timed(log, "hub BFS", T):
        ensure_hubs(cfg, gdir, cand.node_id.tolist())
    with timed(log, "hub metrics", T):
        met = pd.DataFrame([hub_metrics(cfg, h, pairs) for h in cand.node_id])
    hubs = cand.merge(met, on="node_id")
    hubs["title"] = nodes.title.to_numpy()[hubs.node_id]
    hubs["page_id"] = nodes.page_id.to_numpy()[hubs.node_id]
    hubs["is_disambig"] = nodes.is_disambig.to_numpy()[hubs.node_id]
    hubs["out_degree"] = od[hubs.node_id]
    hubs["in_degree"] = idg[hubs.node_id]
    hubs["path_frequency"] = path_freq.reindex(hubs.node_id).fillna(0).astype(int).to_numpy()
    hubs["in_largest_scc"] = lscc[hubs.node_id]
    # A mean detour computed on few connected pairs is not comparable: hubs below the
    # coverage threshold keep their metrics but are not ranked by mean detour.
    min_cov = A["min_coverage"]
    hubs["rankable"] = hubs.coverage >= min_cov
    hubs["mean_detour_ranked"] = hubs.mean_detour.where(hubs.rankable)
    hubs["rank_mean_detour"] = hubs.mean_detour_ranked.rank(method="min")
    hubs["rank_on_path"] = hubs.on_path_rate.rank(ascending=False, method="min")
    hubs["rank_out_degree"] = hubs.out_degree.rank(ascending=False, method="min")
    hubs.sort_values(["mean_detour_ranked", "node_id"], na_position="last").to_csv(out / "hubs.csv", index=False)
    res["hub_coverage"] = {"min_coverage": min_cov, "rankable": int(hubs.rankable.sum()),
                           "total_pairs_s_cannot_reach_h": int(hubs.pairs_s_cannot_reach_h.sum()),
                           "total_pairs_h_cannot_reach_t": int(hubs.pairs_h_cannot_reach_t.sum()),
                           "not_rankable": int((~hubs.rankable).sum()),
                           "not_rankable_titles": hubs.title[~hubs.rankable].tolist()}

    # ---- perceived vs real ---------------------------------------------------------------
    def spear(df, a, b):
        df = df[[a, b]].dropna()
        rho, p = spearmanr(df[a], df[b])
        return {"rho": float(rho), "p_value": float(p), "n": int(len(df))}

    sel = hubs[~hubs.random_baseline | hubs.top_out_degree | hubs.top_in_degree | hubs.top_path_frequency]
    base = hubs[hubs.random_baseline]
    k = A["top_overlap"]
    top_k_out = set(hubs.nsmallest(k, "rank_out_degree").node_id)
    top_k_det = set(hubs.nsmallest(k, "mean_detour_ranked").node_id)
    top_k_on = set(hubs.nlargest(k, "on_path_rate").node_id)
    res["perceived_vs_real"] = {
        "spearman_out_degree_vs_mean_detour_candidates": spear(sel, "out_degree", "mean_detour_ranked"),
        "spearman_out_degree_vs_on_path_candidates": spear(sel, "out_degree", "on_path_rate"),
        "spearman_in_degree_vs_mean_detour_candidates": spear(sel, "in_degree", "mean_detour_ranked"),
        "spearman_in_degree_vs_on_path_candidates": spear(sel, "in_degree", "on_path_rate"),
        "spearman_out_degree_vs_mean_detour_all_evaluated": spear(hubs, "out_degree", "mean_detour_ranked"),
        "spearman_out_degree_vs_mean_detour_random_baseline": spear(base, "out_degree", "mean_detour_ranked"),
        f"top{k}_overlap_out_degree_vs_mean_detour": len(top_k_out & top_k_det),
        f"top{k}_overlap_out_degree_vs_on_path": len(top_k_out & top_k_on),
        "mean_detour_by_group": {
            grp: {"n": int(hubs[hubs[grp]].shape[0]),
                  "n_rankable": int(hubs[hubs[grp]].rankable.sum()),
                  "mean_of_mean_detour": float(hubs[hubs[grp]].mean_detour_ranked.mean()),
                  "median_of_mean_detour": float(hubs[hubs[grp]].mean_detour_ranked.median()),
                  "mean_on_path_rate": float(hubs[hubs[grp]].on_path_rate.mean()),
                  "median_on_path_rate": float(hubs[hubs[grp]].on_path_rate.median())}
            for grp in ["top_out_degree", "top_in_degree", "top_path_frequency", "random_baseline"]},
        # The pooled correlation mixes four groups selected on different variables; the
        # sign can differ inside each group, so each group is also reported on its own.
        "spearman_by_group": {
            grp: {"out_degree_vs_mean_detour": spear(hubs[hubs[grp]], "out_degree", "mean_detour_ranked"),
                  "in_degree_vs_mean_detour": spear(hubs[hubs[grp]], "in_degree", "mean_detour_ranked")}
            for grp in ["top_out_degree", "top_in_degree", "top_path_frequency", "random_baseline"]},
    }
    labels_p = ROOT / "labels" / "hub_type_labels.csv"
    if labels_p.exists():
        PV = res["perceived_vs_real"]
        lab = pd.read_csv(labels_p)[["page_id", "type"]]
        h2 = hubs.merge(lab, on="page_id", how="left")
        typed = h2[~h2.random_baseline | h2.top_out_degree | h2.top_in_degree | h2.top_path_frequency]
        # Labels are needed for the non-random candidates only (pure random-baseline pages
        # never enter a by-type statistic). A missing label must not go unnoticed.
        missing = typed[typed.type.isna()]
        if len(missing):
            log.warning("%d candidate(s) have no type label in %s and are left out of the by-type "
                        "statistics; add them before using this sample: %s", len(missing),
                        labels_p.name, ", ".join(missing.title.head(20)))
        PV["unlabelled_candidates"] = int(len(missing))
        PV["label_rows_not_in_this_sample"] = int((~lab.page_id.isin(hubs.page_id)).sum())
        PV["by_type_label"] = {
            t: {"n": int(len(x)), "n_rankable": int(x.rankable.sum()),
                "median_mean_detour": float(x.mean_detour_ranked.median()),
                "median_on_path_rate": float(x.on_path_rate.median())}
            for t, x in typed.groupby("type")}
        min_n = A["min_type_size"]
        grouped = group_small_types(typed.type, min_n)
        PV["min_type_size"] = min_n
        PV["small_types"] = sorted(set(typed.type.dropna()) - set(grouped.dropna()))
        PV["small_types_n"] = int((grouped == SMALL_TYPES).sum())
        # Reference pages (identifiers, units, taxonomic ranks...) are linked mostly by
        # templates; their in-degree says little about how central they are for a player.
        noref = typed[typed.type != "reference"]
        PV["n_reference_candidates"] = int((typed.type == "reference").sum())
        PV["spearman_in_degree_vs_mean_detour_candidates_without_reference"] = spear(noref, "in_degree", "mean_detour_ranked")
        PV["spearman_out_degree_vs_mean_detour_candidates_without_reference"] = spear(noref, "out_degree", "mean_detour_ranked")
    write_json(res, out / "results.json")
    log.info("results written to %s", out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int)
    ap.add_argument("--n-pairs", type=int)
    a = ap.parse_args()
    main(a.seed, a.n_pairs)
