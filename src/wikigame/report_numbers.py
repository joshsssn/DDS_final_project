"""Write every number and table quoted in the LaTeX report, from the pipeline outputs.

Outputs (reports/generated/):
  numbers.tex          \\newcommand macros, e.g. \\NArticles -> 2,782,511
  table_best_hubs.tex  best candidate hubs (mean detour) with type labels
  table_landmarks.tex  top perceived landmarks (out-degree) and their real quality
  table_storage.tex    storage benchmark comparison
The report never types a number by hand: it uses these macros and tables.
"""
from __future__ import annotations

import pandas as pd

from .common import ROOT, load_config, path, read_json


def fmt_int(x) -> str:
    return f"{int(x):,}".replace(",", "{,}")


def fmt_med(x) -> str:
    """Medians of an even number of values can end in .5: never truncate them."""
    return fmt_int(x) if float(x).is_integer() else f"{x:,.1f}".replace(",", "{,}")


def fmt(x, d=2) -> str:
    return f"{x:.{d}f}"


def pct(x, d=1) -> str:
    return f"{100 * x:.{d}f}\\%"


def tex_escape(s: str) -> str:
    for a, b in [("\\", "\\textbackslash{}"), ("&", "\\&"), ("%", "\\%"), ("$", "\\$"),
                 ("#", "\\#"), ("_", " "), ("{", "\\{"), ("}", "\\}"), ("~", "\\~{}"), ("^", "\\^{}")]:
        s = s.replace(a, b)
    return s


def main() -> None:
    cfg = load_config()
    A = cfg["analysis"]
    gen = path(cfg, "generated")
    sample = gen / f"sample_seed{A['seed']}_n{A['n_pairs']}"
    q = read_json(path(cfg, "processed") / "quality.json")
    ps = read_json(path(cfg, "interim") / "parse_stats.json")
    r = read_json(sample / "results.json")
    hubs = pd.read_csv(sample / "hubs.csv")
    lab_p = ROOT / "labels" / "hub_type_labels.csv"
    if lab_p.exists():
        hubs = hubs.merge(pd.read_csv(lab_p)[["page_id", "type"]], on="page_id", how="left")
    else:
        hubs["type"] = None
    M: dict[str, str] = {}

    # ---- data & cleaning ---------------------------------------------------------
    M["DumpDate"] = cfg["dump"]["date"]
    M["NPagesTotal"] = fmt_int(q["pages_total"])
    M["NLinkRows"] = fmt_int(ps["pagelinks"]["rows_in_dump"])
    M["NLinktarget"] = fmt_int(ps["linktarget"]["rows_in_dump"])
    ns0 = next(x for x in q["pages_per_namespace"] if x["namespace"] == 0)
    M["NNsZeroPages"] = fmt_int(ns0["pages"])
    M["NRedirects"] = fmt_int(ns0["redirects"])
    M["NArticles"] = fmt_int(q["articles"])
    M["NDisambig"] = fmt_int(q["degrees"]["disambiguation_nodes"])
    M["NDoubleRedirects"] = fmt_int(q["redirects"]["double_redirects_(chain_length>=2)"])
    rs = q["redirects"]["status_counts"]
    M["NRedirectsDropped"] = fmt_int(sum(v for k, v in rs.items() if k != "resolved"))
    links = q["links"]["by_source_and_target_class"]
    from_art = sum(x["n"] for x in links if x["source"] == "source_article")
    def art_to(t):
        return sum(x["n"] for x in links if x["source"] == "source_article" and x["target"] == t)
    M["NLinksFromArticles"] = fmt_int(from_art)
    M["ShareLinksFromArticles"] = pct(from_art / q["links"]["rows"])
    M["NRedLinks"] = fmt_int(art_to("red_link"))
    M["ShareRedLinks"] = pct(art_to("red_link") / from_art)
    M["NLinksOtherNs"] = fmt_int(art_to("other_namespace"))
    M["ShareLinksOtherNs"] = pct(art_to("other_namespace") / from_art)
    M["NLinksViaRedirect"] = fmt_int(art_to("redirect_resolved"))
    M["ShareLinksViaRedirect"] = pct(art_to("redirect_resolved") / from_art)
    E = q["edges"]
    M["NCandidateEdges"] = fmt_int(E["candidate_edges"])
    M["NSelfLoops"] = fmt_int(E["self_loops"])
    M["NDuplicates"] = fmt_int(E["duplicates_after_redirect_resolution"])
    M["ShareDuplicates"] = pct(E["duplicates_after_redirect_resolution"] / E["candidate_edges"], 2)
    M["NEdges"] = fmt_int(E["final_edges"])
    D = q["degrees"]
    M["NSinks"] = fmt_int(D["sinks_out_degree_0"])
    M["NOrphans"] = fmt_int(D["orphans_in_degree_0"])
    M["MeanOutDeg"] = fmt(D["mean_out_degree"], 1)
    M["MedianOutDeg"] = fmt_med(D["median_out_degree"])
    M["MedianInDeg"] = fmt_med(D["median_in_degree"])
    M["MaxOutDeg"] = fmt_int(D["max_out_degree"])
    M["MaxInDeg"] = fmt_int(D["max_in_degree"])
    M["ParseSecondsPagelinks"] = fmt(ps["pagelinks"]["seconds"], 0)

    # ---- analysis ------------------------------------------------------------------
    M["NPairs"] = fmt_int(r["n_pairs"])
    M["TopK"] = fmt_int(r["top_k"])
    M["Seed"] = str(r["seed"])
    L = r["largest_scc"]
    M["LsccSize"] = fmt_int(L["size"])
    M["LsccShare"] = pct(L["share_of_nodes"])
    P = r["pairs"]
    M["NReachable"] = fmt_int(P["reachable"])
    M["NUnreachable"] = fmt_int(P["unreachable"])
    M["ShareUnreachable"] = pct(P["unreachable_share"])
    M["MeanDist"] = fmt(P["mean_distance"])
    M["MedianDist"] = fmt_med(P["median_distance"])
    dd = {int(k): v for k, v in P["distance_distribution"].items()}
    M["MaxDistSample"] = fmt_int(max(dd))
    M["ShareDistLeFour"] = pct(sum(v for k, v in dd.items() if k <= 4) / P["reachable"])
    C = r["candidates"]
    M["NCandidates"] = fmt_int(C["union_size"])
    M["NRandomBaseline"] = fmt_int(C["random_baseline"])
    M["OverlapOutIn"] = fmt_int(C["overlap_out_in"])
    M["OverlapOutPath"] = fmt_int(C["overlap_out_path"])
    M["OverlapInPath"] = fmt_int(C["overlap_in_path"])
    M["PathFreqKth"] = fmt_int(C["path_frequency_of_kth_candidate"])
    PV = r["perceived_vs_real"]
    for key, name in [("spearman_out_degree_vs_mean_detour_candidates", "RhoOutDetour"),
                      ("spearman_out_degree_vs_on_path_candidates", "RhoOutOnPath"),
                      ("spearman_in_degree_vs_mean_detour_candidates", "RhoInDetour"),
                      ("spearman_in_degree_vs_on_path_candidates", "RhoInOnPath"),
                      ("spearman_out_degree_vs_mean_detour_all_evaluated", "RhoOutDetourAll"),
                      ("spearman_out_degree_vs_mean_detour_random_baseline", "RhoOutDetourRandom")]:
        M[name] = fmt(PV[key]["rho"])
        M[name + "P"] = f"{PV[key]['p_value']:.2g}"
        M[name + "N"] = fmt_int(PV[key]["n"])
    k = A["top_overlap"]
    M["TopOverlapK"] = str(k)
    M["TopOverlapOutDetour"] = fmt_int(PV[f"top{k}_overlap_out_degree_vs_mean_detour"])
    M["TopOverlapOutOnPath"] = fmt_int(PV[f"top{k}_overlap_out_degree_vs_on_path"])
    for grp, name in [("top_out_degree", "GrpOut"), ("top_in_degree", "GrpIn"),
                      ("top_path_frequency", "GrpPath"), ("random_baseline", "GrpRandom")]:
        g = PV["mean_detour_by_group"][grp]
        M[name + "MedDetour"] = fmt(g["median_of_mean_detour"])
        M[name + "OnPath"] = pct(g["mean_on_path_rate"])
    M["NotRankable"] = fmt_int(r["hub_coverage"]["not_rankable"])
    M["MinCoverage"] = pct(r["hub_coverage"]["min_coverage"], 0)
    M["NUnreachTOrphan"] = fmt_int(P["unreachable_breakdown"]["t_is_orphan"])
    M["NUnreachTOutside"] = fmt_int(P["unreachable_breakdown"]["t_outside_lscc"])
    M["NUnreachSOutside"] = fmt_int(P["unreachable_breakdown"]["s_outside_lscc"])
    M["NUnreachSSink"] = fmt_int(P["unreachable_breakdown"]["s_is_sink"])
    M["NDistGeEight"] = fmt_int(sum(v for k, v in dd.items() if k >= 8))
    M["NDistinctOnPaths"] = fmt_int(C["distinct_intermediate_nodes_on_sampled_paths"])
    M["PathFreqTies"] = fmt_int(C["path_frequency_ties_at_kth"])
    for grp, name in [("top_out_degree", "GrpOut"), ("top_in_degree", "GrpIn"),
                      ("top_path_frequency", "GrpPath"), ("random_baseline", "GrpRandom")]:
        M[name + "MedOnPath"] = pct(PV["mean_detour_by_group"][grp]["median_on_path_rate"], 2)
    # pages of the sexagenary (Chinese) calendar labelled 'period': a distinct cluster
    sexa = hubs[hubs.title.str.match(r"^(Rat|Buffle|Tigre|Lièvre|Dragon|Serpent|Cheval|Chèvre|Singe|Coq|Chien|Cochon)_d")]
    M["NSexagenary"] = fmt_int(len(sexa))
    if len(sexa):
        M["SexagenaryMedOutDeg"] = fmt_med(sexa.out_degree.median())
        M["SexagenaryMedDetour"] = fmt(sexa.mean_detour.median())
        M["SexagenaryMedInDeg"] = fmt_med(sexa.in_degree.median())
    # composition of the top 15 by type, e.g. "10 country, 3 city, 1 continent, 1 period"
    top15 = hubs.sort_values("mean_detour").head(15)["type"].fillna("unlabelled").value_counts()
    M["TopFifteenTypes"] = ", ".join(f"{v} {k}" for k, v in top15.items())
    M["NBfsRuns"] = fmt_int(r["n_pairs"] + 2 * C["union_size"])
    man = read_json(path(cfg, "raw") / "manifest.json")
    M["RawGB"] = fmt(sum(v["size_bytes"] for v in man.values()) / 1e9, 2)
    best = hubs.sort_values("mean_detour").iloc[0]
    M["BestHub"] = tex_escape(best.title)
    M["BestHubDetour"] = fmt(best.mean_detour)
    M["BestHubOnPath"] = pct(best.on_path_rate)
    topout = hubs.sort_values("out_degree", ascending=False).iloc[0]
    M["TopOutHub"] = tex_escape(topout.title)
    M["TopOutHubDetour"] = fmt(topout.mean_detour)
    M["TopOutHubRank"] = fmt_int(topout.rank_mean_detour)
    if "by_type_label" in PV:
        for t, v in PV["by_type_label"].items():
            key = "".join(w.capitalize() for w in str(t).replace("/", " ").replace("-", " ").split())
            M[f"Type{key}N"] = fmt_int(v["n"])
            M[f"Type{key}MedDetour"] = fmt(v["median_mean_detour"])

    # ---- bias check ----------------------------------------------------------------
    bc_p = gen / "bias_check.json"
    if bc_p.exists():
        B = read_json(bc_p)
        for grp, name in [("top_out_degree", "BiasTop"), ("top_in_degree", "BiasIn"), ("random", "BiasRandom")]:
            if grp in B:
                M[name + "N"] = fmt_int(B[grp]["n"])
                M[name + "MedShare"] = pct(B[grp]["median_template_share"])
                M[name + "MeanShare"] = pct(B[grp]["mean_template_share"])
                M[name + "MedAll"] = fmt_med(B[grp]["median_html_all"])
                M[name + "MedBody"] = fmt_med(B[grp]["median_body"])
                M[name + "MedDump"] = fmt_med(B[grp]["median_dump_out_degree"])
        M["BiasRho"] = fmt(B["top_group_spearman_dump_vs_body"]["rho"])
        M["BiasErrors"] = fmt_int(B["n_errors"])
        M["BiasNPages"] = fmt_int(B["n_pages"])
        bc = pd.read_csv(gen / "bias_check.csv")
        ratio = (bc.html_all / bc.dump_out_degree).dropna()
        M["BiasRatioMed"] = fmt(ratio.median(), 3)
        M["BiasRatioMin"] = fmt(ratio.min(), 3)
        M["BiasRatioMax"] = fmt(ratio.max(), 3)

    # ---- storage ---------------------------------------------------------------------
    sb_p = gen / "storage_benchmark.json"
    if sb_p.exists():
        S = read_json(sb_p)
        for opt, name in [("csr", "Csr"), ("duckdb", "Duck")]:
            s = S[opt]
            M[f"{name}LoadS"] = fmt(s["load_s"], 2 if s["load_s"] < 1 else 1)
            M[f"{name}BfsS"] = fmt(s["q1_fwd_bfs_s"], 1)
            M[f"{name}RevBfsS"] = fmt(s["q2_rev_bfs_s"], 1)
            M[f"{name}DiskGB"] = fmt(s["disk_bytes"] / 1e9, 2)
            M[f"{name}RamGB"] = fmt(s["rss_peak_mb"] / 1024, 1)
        M["DuckBfsRatio"] = fmt(S["duckdb"]["q1_fwd_bfs_s"] / S["csr"]["q1_fwd_bfs_s"], 1)
        M["DuckRevBfsRatio"] = fmt(S["duckdb"]["q2_rev_bfs_s"] / S["csr"]["q2_rev_bfs_s"], 1)
        write_storage_table(S, gen / "table_storage.tex")

    # ---- robustness, throughput, cost of evaluating every pair --------------------
    from .slide_figures import pair_throughput, robustness, seed_comparison
    sc = seed_comparison(gen, cfg)
    if sc is not None:
        other = read_json(gen / f"sample_seed{A['robustness_seed']}_n{A['n_pairs']}" / "results.json")
        oh = pd.read_csv(gen / f"sample_seed{A['robustness_seed']}_n{A['n_pairs']}" / "hubs.csv")
        M["OtherSeed"] = str(A["robustness_seed"])
        M["SeedN"] = fmt_int(len(sc))
        M["SeedRho"] = fmt(sc.mean_detour_main.corr(sc.mean_detour_other, method="spearman"), 5)
        M["SeedRhoOnPath"] = fmt(sc.on_path_rate_main.corr(sc.on_path_rate_other, method="spearman"), 4)
        M["SeedMaxDiff"] = fmt((sc.mean_detour_main - sc.mean_detour_other).abs().max(), 3)
        top_a = set(hubs.sort_values("mean_detour").head(15).node_id)
        top_b = set(oh.sort_values("mean_detour").head(15).node_id)
        M["SeedTopFifteenShared"] = fmt_int(len(top_a & top_b))
        same = (hubs.sort_values("mean_detour").head(15).node_id.tolist()
                == oh.sort_values("mean_detour").head(15).node_id.tolist())
        M["SeedTopFifteenOrder"] = "in the same order" if same else "in a slightly different order"
        ob = oh.sort_values("mean_detour").iloc[0]
        M["OtherBestHub"] = tex_escape(ob.title)
        M["OtherBestHubDetour"] = fmt(ob.mean_detour)
        M["OtherBestHubOnPath"] = pct(ob.on_path_rate)
        M["OtherNCandidates"] = fmt_int(other["candidates"]["union_size"])
        M["OtherMeanDist"] = fmt(other["pairs"]["mean_distance"])
        M["OtherShareUnreachable"] = pct(other["pairs"]["unreachable_share"])
        opv = other["perceived_vs_real"]
        M["OtherRhoOutDetour"] = fmt(opv["spearman_out_degree_vs_mean_detour_candidates"]["rho"])
        M["OtherRhoInDetour"] = fmt(opv["spearman_in_degree_vs_mean_detour_candidates"]["rho"])
        both_cand = set(hubs.node_id[~hubs.random_baseline]) & set(oh.node_id[~oh.random_baseline])
        M["SeedSharedCandidates"] = fmt_int(len(both_cand))
    rb = robustness(gen, cfg)
    if rb is not None:
        M["RobustN"] = fmt_int(len(rb))
        M["RobustRho"] = fmt(rb.mean_detour_small.corr(rb.mean_detour_big, method="spearman"), 4)
        M["RobustMaxDiff"] = fmt((rb.mean_detour_small - rb.mean_detour_big).abs().max(), 3)
    tp = pair_throughput(cfg)
    if tp is not None:
        old_rate, new_rate = tp["old"][0] / tp["old"][1], tp["new"][0] / tp["new"][1]
        M["PairsOldN"], M["PairsOldSeconds"] = fmt_int(tp["old"][0]), fmt(tp["old"][1], 0)
        M["PairsNewN"], M["PairsNewSeconds"] = fmt_int(tp["new"][0]), fmt(tp["new"][1], 0)
        M["PairRateOld"], M["PairRateNew"] = fmt(old_rate, 1), fmt(new_rate, 0)
        M["PairSpeedup"] = fmt_int(round(new_rate / old_rate))   # wall-clock; 4 vs 6 processes
        n_elig = q["articles"] - q["degrees"]["disambiguation_nodes"]
        all_pairs = n_elig * (n_elig - 1)
        M["NEligible"] = fmt_int(n_elig)
        M["AllPairsTrillions"] = fmt(all_pairs / 1e12, 1)
        M["AllPairsYearsBidir"] = fmt_int(round(all_pairs / new_rate / 86400 / 365))
        import re as _re
        txt = (path(cfg, "processed") / "analysis_n200000.log").read_text(encoding="utf-8", errors="replace")
        hb = _re.findall(r"hubs: \d+ requested, (\d+) to compute", txt)
        hs = _re.search(r"done: hub BFS in ([\d.]+) s", txt)
        if hb and hs and int(hb[-1]) > 0:
            per_bfs = float(hs.group(1)) / (2 * int(hb[-1]))    # wall seconds per full BFS
            M["FullBfsWallS"] = fmt(per_bfs, 1)
            M["AllPairsDaysBfs"] = fmt_int(round(n_elig * per_bfs / 86400))
            M["AllPairsTB"] = fmt(all_pairs / 1e12, 1)

    with open(gen / "numbers.tex", "w", encoding="utf-8") as f:
        f.write("% Generated by src/wikigame/report_numbers.py -- do not edit by hand.\n")
        for k_, v in M.items():
            f.write(f"\\newcommand{{\\{k_}}}{{{v}}}\n")

    write_hub_table(hubs.sort_values("mean_detour").head(15), gen / "table_best_hubs.tex")
    write_hub_table(hubs.sort_values("out_degree", ascending=False).head(10), gen / "table_landmarks.tex")
    print(f"wrote {len(M)} macros to {gen / 'numbers.tex'}")


def write_hub_table(df, out):
    lines = ["\\begin{tabular}{@{}rlp{1.5cm}rrrr@{}}", "\\toprule",
             "\\# & Page & Type & Out-deg. & In-deg. & Mean detour & On s.p. \\\\", "\\midrule"]
    for i, r in enumerate(df.itertuples(), 1):
        t = r.type if isinstance(r.type, str) else "--"
        lines.append(f"{i} & {tex_escape(r.title)[:42]} & {tex_escape(t)} & {fmt_int(r.out_degree)} & "
                     f"{fmt_int(r.in_degree)} & {r.mean_detour:.2f} & {100 * r.on_path_rate:.1f}\\% \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_storage_table(S, out):
    c, d = S["csr"], S["duckdb"]
    rows = [
        ("Build from cleaned edges (s)", f"{c['build_s']:.0f}", f"{d.get('build_s', float('nan')):.0f}"),
        ("Disk size (GB)", f"{c['disk_bytes'] / 1e9:.2f}", f"{d['disk_bytes'] / 1e9:.2f}"),
        ("Load / connect (s)", f"{c['load_s']:.1f}", f"{d['load_s']:.2f}"),
        ("Peak resident memory (GB)", f"{c['rss_peak_mb'] / 1024:.1f}", f"{d['rss_peak_mb'] / 1024:.1f}"),
        ("Q1 full forward BFS (s)", f"{c['q1_fwd_bfs_s']:.1f}", f"{d['q1_fwd_bfs_s']:.1f}"),
        ("Q2 full reverse BFS (s)", f"{c['q2_rev_bfs_s']:.1f}", f"{d['q2_rev_bfs_s']:.1f}"),
        ("Q3 degrees of 1000 nodes (ms)", f"{1000 * c['q3_degrees_s']:.2f}", f"{1000 * d['q3_degrees_s']:.1f}"),
        ("Q4 2000 title/id lookups (ms)", f"{1000 * c['q4_lookups_s']:.1f}", f"{1000 * d['q4_lookups_s']:.0f}"),
    ]
    lines = ["\\begin{tabular}{@{}lrr@{}}", "\\toprule",
             "Measure & NumPy CSR + Parquet & DuckDB \\\\", "\\midrule"]
    lines += [f"{a} & {b} & {e} \\\\" for a, b, e in rows]
    lines += ["\\bottomrule", "\\end{tabular}"]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
