"""Figures designed for the 16:9 slides (reports/figures/slides/, PDF + PNG).

Same rule as the report figures: every value is read from files written by the
pipeline. Sizes are the real size on a 16 cm x 9 cm slide, so fonts are not rescaled.
"""
from __future__ import annotations

import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .common import ROOT, SMALL_TYPES, get_logger, load_config, path, read_json
from .figures import (AQUA, BLUE, GREY, GRID, GROUP_STYLE, INK, INK2, ORANGE, hub_groups, nice,
                      typed_candidates, type_order,
                      save)

log = get_logger("slide_figures")
YELLOW, LIGHT_BLUE, LIGHT_GREY = "#eda100", "#86b6ef", "#c9c8c3"
TYPE_COLOR = {"country": BLUE, "city": ORANGE, "continent": AQUA, "period": YELLOW, "list": GREY}


def millions(x) -> str:
    return f"{x / 1e6:.1f} M"


def fig_funnel(q, figdir):
    """From raw link rows to graph edges."""
    links = q["links"]["by_source_and_target_class"]
    from_art = sum(x["n"] for x in links if x["source"] == "source_article")
    stages = [("All rows of pagelinks", q["links"]["rows"]),
              ("Source is an article", from_art),
              ("Target is an article", q["edges"]["candidate_edges"]),
              ("Edges of the graph", q["edges"]["final_edges"])]
    fig, ax = plt.subplots(figsize=(3.45, 1.5))
    names = [s[0] for s in stages][::-1]
    vals = [s[1] for s in stages][::-1]
    colors = [BLUE] + [LIGHT_BLUE] * 3
    ax.barh(names, vals, color=colors, height=0.62)
    for i, v in enumerate(vals):
        ax.text(v, i, f"  {millions(v)}", va="center", fontsize=7.5, color=INK,
                fontweight="bold" if i == 0 else "normal")
    ax.set_xlim(0, max(vals) * 1.17)
    ax.xaxis.set_visible(False)
    ax.grid(False)
    for s in ("bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(axis="y", length=0, labelsize=7.5)
    save(fig, "s_funnel", figdir)


def fig_targets(q, figdir):
    """Where do the links of articles point? One stacked bar, kept vs dropped."""
    links = q["links"]["by_source_and_target_class"]
    def n(t):
        return sum(x["n"] for x in links if x["source"] == "source_article" and x["target"] == t)
    total = sum(x["n"] for x in links if x["source"] == "source_article")
    parts = [("article", n("article"), BLUE, "white"),
             ("via redirect", n("redirect_resolved"), LIGHT_BLUE, INK),
             ("red link", n("red_link"), GREY, "white"),
             ("other namespace", n("other_namespace"), LIGHT_GREY, INK)]
    fig, ax = plt.subplots(figsize=(3.45, 0.85))
    left = 0
    for name, v, color, ink in parts:
        share = v / total
        ax.barh([0], [share], left=left, color=color, height=0.5, edgecolor="white", linewidth=1.5)
        left += share
    # legend row under the bar: one coloured square + label + share per part
    for k, (name, v, color, ink) in enumerate(parts):   # 2 x 2 grid
        x, y = (0.0, 0.5)[k // 2], (-0.5, -0.9)[k % 2]
        ax.add_patch(plt.Rectangle((x, y - 0.12), 0.022, 0.24, color=color, clip_on=False))
        ax.text(x + 0.035, y, f"{name}: {100 * v / total:.1f}%", fontsize=6.8, color=INK, va="center")
    kept = (parts[0][1] + parts[1][1]) / total
    ax.text(kept / 2, 0.36, "kept", fontsize=6.5, color=BLUE, ha="center", va="bottom", fontweight="bold")
    ax.text(kept + (1 - kept) / 2, 0.36, "dropped", fontsize=6.5, color=INK2, ha="center", va="bottom")
    ax.set_xlim(0, 1)
    ax.set_ylim(-1.1, 0.7)
    ax.axis("off")
    save(fig, "s_targets", figdir)


def fig_storage(S, figdir):
    c, d = S["csr"], S["duckdb"]
    fig, axes = plt.subplots(1, 2, figsize=(3.45, 2.2), gridspec_kw={"wspace": 0.38})
    for ax, title, keys, scale, unit, logy in [
            (axes[0], "Full BFS (seconds)", [("forward", "q1_fwd_bfs_s"), ("reverse", "q2_rev_bfs_s")], 1, "s", False),
            (axes[1], "Lookups (ms, log scale)",
             [("degrees", "q3_degrees_s"), ("title / id", "q4_lookups_s")], 1000, "ms", True)]:
        x = np.arange(len(keys))
        for off, opt, color, name in [(-0.19, c, BLUE, "NumPy CSR"), (0.19, d, ORANGE, "DuckDB")]:
            vals = [opt[k] * scale for _, k in keys]
            ax.bar(x + off, vals, width=0.34, color=color, label=name)
            for xi, v in zip(x + off, vals):
                ax.text(xi, v * (1.12 if logy else 1) + (0 if logy else 0.2),
                        f"{v:.1f}" if v >= 1 else f"{v:.2f}", ha="center", va="bottom",
                        fontsize=7, color=INK)
        ax.set_xticks(x, [k for k, _ in keys])
        ax.set_title(title, fontsize=7.5, loc="left")
        ax.grid(axis="x", visible=False)
        if logy:
            ax.set_yscale("log")
            ax.set_ylim(0.02, 9000)
        else:
            ax.set_ylim(0, max(d[k] for _, k in keys) * 1.5)
    axes[0].legend(loc="upper left", fontsize=7)
    save(fig, "s_storage", figdir)


def pair_throughput(cfg):
    """(pairs computed, seconds) of the one-sided run and of the bidirectional run, from logs."""
    out = {}
    for key, name in [("old", "analysis.log"), ("new", "analysis_n200000.log")]:
        p = path(cfg, "processed") / name
        if not p.exists():
            return None
        txt = p.read_text(encoding="utf-8", errors="replace")
        n = re.search(r"pairs: \d+ in sample, \d+ cached, (\d+) to compute", txt)
        s = re.search(r"done: pairs in ([\d.]+) s", txt)
        if not (n and s) or int(n.group(1)) == 0:
            return None
        out[key] = (int(n.group(1)), float(s.group(1)))
    return out


def fig_speed(cfg, figdir):
    t = pair_throughput(cfg)
    if t is None:
        log.warning("analysis logs not found: skipping s_speed")
        return
    rates = [t["old"][0] / t["old"][1], t["new"][0] / t["new"][1]]
    names = ["One-sided BFS\n(first run)", "Bidirectional BFS"]
    fig, ax = plt.subplots(figsize=(2.05, 2.0))
    ax.bar(names, rates, color=[GREY, BLUE], width=0.55)
    for i, r in enumerate(rates):
        ax.text(i, r, f"{r:,.0f}" if r >= 10 else f"{r:.1f}", ha="center", va="bottom",
                fontsize=8.5, fontweight="bold", color=INK)
    ax.set_ylabel("pairs per second")
    ax.set_ylim(0, max(rates) * 1.18)
    ax.grid(axis="x", visible=False)
    ax.tick_params(axis="x", labelsize=7)
    save(fig, "s_speed", figdir)


def fig_best_hubs(h, figdir, n=12):
    top = h.nlargest(n, "on_path_rate").iloc[::-1]
    fig, ax = plt.subplots(figsize=(3.95, 2.55))
    colors = [TYPE_COLOR.get(t, GREY) for t in top.type]
    ax.barh([nice(t) for t in top.title], top.on_path_rate * 100, color=colors, height=0.66)
    for i, (onp, det) in enumerate(zip(top.on_path_rate, top.mean_detour)):
        ax.text(onp * 100, i, f"  {100 * onp:.1f}%   (detour {det:.2f})", va="center",
                fontsize=6.5, color=INK2)
    ax.set_xlabel("share of random pairs for which the page is on a shortest path (%)")
    ax.set_xlim(0, top.on_path_rate.max() * 100 * 1.35)
    ax.grid(axis="y", visible=False)
    ax.tick_params(axis="y", labelsize=7.5, length=0)
    seen = [t for t in TYPE_COLOR if t in set(top.type)]
    ax.legend(handles=[plt.Rectangle((0, 0), 1, 1, color=TYPE_COLOR[t]) for t in seen],
              labels=seen, loc="lower right", fontsize=7, title="type label", title_fontsize=7)
    save(fig, "s_best_hubs", figdir)


def fig_two_scatters(h, res, figdir):
    h = h.assign(group=hub_groups(h))
    pv = res["perceived_vs_real"]
    # rho inside each candidate list (pooling lists selected on different variables misleads)
    bg = {g: v["out_degree_vs_mean_detour"]["rho"] for g, v in pv["spearman_by_group"].items()}
    noref = pv.get("spearman_in_degree_vs_mean_detour_candidates_without_reference")
    titles = {
        "out_degree": f"ρ per list: {bg['top_out_degree']:+.2f} top out,\n{bg['top_in_degree']:+.2f} top in, "
                      f"{bg['top_path_frequency']:+.2f} path, {bg['random_baseline']:+.2f} random",
        "in_degree": f"ρ = {pv['spearman_in_degree_vs_mean_detour_candidates']['rho']:+.2f} (all),"
                     + (f"\n{noref['rho']:+.2f} w/o reference pages" if noref else ""),
    }
    fig, axes = plt.subplots(1, 2, figsize=(3.85, 2.5), sharey=True, gridspec_kw={"wspace": 0.06})
    for ax, col, key, label in [
            (axes[0], "out_degree", "spearman_out_degree_vs_mean_detour_candidates", "out-degree"),
            (axes[1], "in_degree", "spearman_in_degree_vs_mean_detour_candidates", "in-degree")]:
        for grp, (color, marker) in GROUP_STYLE.items():
            x = h[h.group == grp]
            ax.scatter(x[col].clip(lower=1), x.mean_detour, s=11, color=color, marker=marker,
                       edgecolor="white", linewidth=0.4, label=grp, zorder=3)
        ax.set_xscale("log")
        ax.set_xlabel(label + " (log scale)", fontsize=7.5)
        ax.set_title(titles[col].replace("-", "−"), fontsize=6,
                     loc="left" if col == "out_degree" else "right")
        fr = h.nsmallest(1, "mean_detour").iloc[0]
        ax.annotate(nice(fr.title), (fr[col], fr.mean_detour), fontsize=6.5, color=INK2,
                    xytext=(-5, 0), textcoords="offset points", ha="right", va="center")
    axes[0].set_ylabel("mean detour (extra clicks)")
    axes[1].legend(loc="upper right", fontsize=5.5, handletextpad=0.1, borderpad=0.2, labelspacing=0.25)
    save(fig, "s_two_scatters", figdir)


def fig_bias_bars(B, figdir):
    rows = [("Random articles", "random"), ("Highest in-degree", "top_in_degree"),
            ("Highest out-degree", "top_out_degree")]
    rows = [(n, B[k]["median_template_share"] * 100, B[k]["n"]) for n, k in rows if k in B][::-1]
    fig, ax = plt.subplots(figsize=(2.9, 1.9))
    ax.barh([f"{n}\nn = {k}" for n, _, k in rows], [v for _, v, _ in rows], color=BLUE, height=0.6)
    for i, (_, v, _) in enumerate(rows):
        ax.text(v, i, f"  {v:.1f}%", va="center", fontsize=8, fontweight="bold", color=INK)
    ax.set_xlim(0, max(v for _, v, _ in rows) * 1.35)
    ax.set_xlabel("median share of links that come\nfrom navigation boxes", fontsize=7)
    ax.grid(axis="y", visible=False)
    ax.tick_params(axis="y", labelsize=6.5, length=0)
    save(fig, "s_bias_bars", figdir)


def robustness(gen, cfg):
    """Hubs evaluated both in the first 2,000-pair sample and in the official one."""
    A = cfg["analysis"]
    small = gen / f"sample_seed{A['seed']}_n2000" / "hubs.csv"
    big = gen / f"sample_seed{A['seed']}_n{A['n_pairs']}" / "hubs.csv"
    if A["n_pairs"] == 2000 or not small.exists() or not big.exists():
        return None
    a = pd.read_csv(small)[["node_id", "mean_detour"]]
    b = pd.read_csv(big)[["node_id", "mean_detour"]]
    return a.merge(b, on="node_id", suffixes=("_small", "_big"))


def seed_comparison(gen, cfg):
    """Hubs evaluated with the main seed and with the independent robustness seed
    (same number of pairs, disjoint pairs). Returns the merged table or None."""
    A = cfg["analysis"]
    a = gen / f"sample_seed{A['seed']}_n{A['n_pairs']}" / "hubs.csv"
    b = gen / f"sample_seed{A.get('robustness_seed')}_n{A['n_pairs']}" / "hubs.csv"
    if not (a.exists() and b.exists()):
        return None
    cols = ["node_id", "title", "mean_detour", "on_path_rate", "rank_mean_detour"]
    return pd.read_csv(a)[cols].merge(pd.read_csv(b)[cols], on=["node_id", "title"], suffixes=("_main", "_other"))


def fig_robustness(gen, cfg, figdir):
    m = seed_comparison(gen, cfg)
    if m is None:
        log.warning("no robustness sample: skipping s_robustness")
        return
    A = cfg["analysis"]
    m = m.rename(columns={"mean_detour_main": "mean_detour_big", "mean_detour_other": "mean_detour_small"})
    rho = m.mean_detour_small.corr(m.mean_detour_big, method="spearman")
    fig, ax = plt.subplots(figsize=(2.3, 2.25))
    lim = [0.7, max(m.mean_detour_small.max(), m.mean_detour_big.max()) + 0.2]
    ax.plot(lim, lim, color=GREY, lw=1, ls="--", zorder=1)
    ax.scatter(m.mean_detour_small, m.mean_detour_big, s=9, color=BLUE, edgecolor="white",
               linewidth=0.3, zorder=3)
    ax.set_xlabel(f"mean detour, seed {A['robustness_seed']}", fontsize=7.5)
    ax.set_ylabel(f"mean detour, seed {A['seed']}", fontsize=7.5)
    ax.set_title(f"{len(m)} hubs in both samples\nSpearman ρ = {rho:.5f}", fontsize=7.5, loc="left")
    ax.set_xlim(lim)
    ax.set_ylim(lim)
    save(fig, "s_robustness", figdir)


def fig_types(h, figdir, min_n):
    """Median mean detour per type label; types with < min_n candidates merged, shown last."""
    c = typed_candidates(h, min_n)
    g = c.groupby("type").mean_detour.agg(["median", "count"])
    g = g.loc[type_order(c)[::-1]]   # barh draws bottom-up: best type on top, small types at the bottom
    base = h[h.random_baseline & ~(h.top_out_degree | h.top_in_degree | h.top_path_frequency)]
    fig, ax = plt.subplots(figsize=(3.85, 2.5))
    ax.barh([f"{t}  (n={n}, not ranked)" if t == SMALL_TYPES else f"{t}  (n={n})"
             for t, n in zip(g.index, g["count"])], g["median"], color=BLUE, height=0.62)
    for i, v in enumerate(g["median"]):
        ax.text(v, i, f"  {v:.2f}", va="center", fontsize=7.5, color=INK)
    b = base.mean_detour.median()
    ax.axvline(b, color=GREY, lw=1.3, ls="--")
    ax.text(b, len(g) - 0.35, "median of random articles ", ha="right", va="bottom", fontsize=6.5, color=INK2)
    ax.set_xlabel("median of the mean detour (extra clicks), lower is better")
    ax.set_xlim(0, b * 1.08)
    ax.grid(axis="y", visible=False)
    ax.tick_params(axis="y", labelsize=7.5, length=0)
    save(fig, "s_types", figdir)


def fig_distance(res, figdir):
    p = res["pairs"]
    dist = {int(k): v for k, v in p["distance_distribution"].items()}
    cap = 8
    ks = list(range(1, min(max(dist), cap - 1) + 1))
    names = [str(k) for k in ks]
    vals = [dist.get(k, 0) / p["n"] for k in ks]
    if max(dist) >= cap:
        names.append(f"{cap}+")
        vals.append(sum(v for k, v in dist.items() if k >= cap) / p["n"])
    names.append("none")
    vals.append(p["unreachable"] / p["n"])
    fig, ax = plt.subplots(figsize=(3.2, 2.3))
    ax.bar(names, [100 * v for v in vals], color=[BLUE] * (len(vals) - 1) + [GREY], width=0.7)
    for i, v in enumerate(vals):
        if v >= 0.004:
            ax.text(i, 100 * v, f"{100 * v:.1f}%", ha="center", va="bottom", fontsize=6.5, color=INK2)
    ax.set_xlabel("clicks on the shortest path (none = no path)")
    ax.set_ylabel("share of random pairs (%)")
    ax.grid(axis="x", visible=False)
    save(fig, "s_distance", figdir)


def main() -> None:
    plt.rcParams.update({"font.size": 7.5, "axes.titlesize": 8, "axes.labelsize": 7.5,
                         "xtick.labelsize": 7, "ytick.labelsize": 7, "legend.fontsize": 6.5})
    cfg = load_config()
    A = cfg["analysis"]
    gen = path(cfg, "generated")
    figdir = path(cfg, "figures") / "slides"
    figdir.mkdir(exist_ok=True)
    sample = gen / f"sample_seed{A['seed']}_n{A['n_pairs']}"
    q = read_json(path(cfg, "processed") / "quality.json")
    res = read_json(sample / "results.json")
    hubs = pd.read_csv(sample / "hubs.csv")
    lab_p = ROOT / "labels" / "hub_type_labels.csv"
    hubs = hubs.merge(pd.read_csv(lab_p)[["page_id", "type"]], on="page_id", how="left") \
        if lab_p.exists() else hubs.assign(type=None)
    fig_funnel(q, figdir)
    fig_distance(res, figdir)
    fig_targets(q, figdir)
    fig_best_hubs(hubs, figdir)
    fig_two_scatters(hubs, res, figdir)
    fig_types(hubs, figdir, A["min_type_size"])
    fig_speed(cfg, figdir)
    fig_robustness(gen, cfg, figdir)
    if (gen / "storage_benchmark.json").exists():
        fig_storage(read_json(gen / "storage_benchmark.json"), figdir)
    if (gen / "bias_check.json").exists():
        fig_bias_bars(read_json(gen / "bias_check.json"), figdir)


if __name__ == "__main__":
    main()
