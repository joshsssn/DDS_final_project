"""Phase 5 - figures for the report (static PNG + PDF in reports/figures/).

Every figure is computed from files written by the pipeline (results.json, hubs.csv,
quality.json, bias_check.csv, degree_counts.csv, labels CSV); nothing is typed in.
All inputs are versioned, so `run.py figures` works on a fresh copy of the repository.
Colours: a colour-blind-friendly palette, at most 3 hues per scatter; each group also
has its own marker shape and a legend entry.
"""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .common import ROOT, SMALL_TYPES, get_logger, group_small_types, load_config, path, read_json

log = get_logger("figures")

BLUE, ORANGE, AQUA, GREY = "#2a78d6", "#eb6834", "#1baf7a", "#8f8e89"
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e4e3df"

plt.rcParams.update({
    "figure.dpi": 150, "savefig.dpi": 200, "savefig.bbox": "tight",
    "font.size": 9, "axes.titlesize": 10, "axes.labelsize": 9,
    "axes.edgecolor": INK2, "axes.labelcolor": INK, "text.color": INK,
    "xtick.color": INK2, "ytick.color": INK2,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
    "axes.axisbelow": True, "legend.frameon": False,
})


def save(fig, name, figdir):
    for ext in ("png", "pdf"):
        fig.savefig(figdir / f"{name}.{ext}")
    plt.close(fig)
    log.info("wrote %s", name)


def nice(title: str) -> str:
    return title.replace("_", " ")


def load_degree_counts(cfg) -> pd.DataFrame | None:
    """Articles per degree value. Recomputed from the node table when the graph has been
    built (and the versioned CSV refreshed); otherwise read from the versioned CSV, so the
    figures can be redrawn from a fresh copy of the repository without the dumps."""
    from .clean import degree_counts
    csv = path(cfg, "generated") / "degree_counts.csv"
    nodes_p = path(cfg, "processed") / "graph" / "nodes.parquet"
    if nodes_p.exists():
        c = degree_counts(pd.read_parquet(nodes_p, columns=["out_degree", "in_degree"]))
        c.to_csv(csv, index=False)
        return c
    if csv.exists():
        log.info("graph not built: degree figure drawn from %s", csv.name)
        return pd.read_csv(csv)
    log.warning("neither %s nor %s found: skipping fig_degree_ccdf", nodes_p, csv)
    return None


def fig_degree_ccdf(counts, figdir):
    fig, ax = plt.subplots(figsize=(5.2, 3.4))
    for col, color, label in [("n_out", BLUE, "out-degree (links on the page)"),
                              ("n_in", ORANGE, "in-degree (pages linking to it)")]:
        n_all = counts[col].sum()   # denominator: ALL articles, including degree 0
        c = counts[(counts.degree > 0) & (counts[col] > 0)].sort_values("degree")
        # degree 0 cannot be drawn on a log axis; ccdf(k) = share of articles with degree >= k
        ccdf = c[col].to_numpy()[::-1].cumsum()[::-1] / n_all
        ax.loglog(c.degree.to_numpy(), ccdf, color=color, lw=2, label=label)
    ax.set_xlabel("degree k (number of distinct article links)")
    ax.set_ylabel("share of articles with degree ≥ k")
    ax.set_title("Degree distributions of the cleaned article graph (CCDF)")
    ax.legend(loc="lower left")
    save(fig, "fig_degree_ccdf", figdir)


def fig_distance_distribution(res, figdir):
    p = res["pairs"]
    dist = {int(k): v for k, v in p["distance_distribution"].items()}
    cap = 8  # distances >= cap are rare: grouped in one bar so the axis stays readable
    ks = list(range(1, min(max(dist), cap - 1) + 1))
    vals = [dist.get(k, 0) / p["n"] for k in ks]
    names = [str(k) for k in ks]
    if max(dist) >= cap:
        names.append(f"{cap}–{max(dist)}")
        vals.append(sum(v for k, v in dist.items() if k >= cap) / p["n"])
    fig, ax = plt.subplots(figsize=(5.2, 3.2))
    ax.bar(names, vals, color=BLUE, width=0.7)
    ax.bar(["∞"], [p["unreachable"] / p["n"]], color=GREY, width=0.7)
    for i, v in enumerate(vals + [p["unreachable"] / p["n"]]):
        if v > 0:
            ax.text(i, v, f"{100 * v:.1f}%" if v >= 0.001 else f"{100 * v:.2f}%",
                    ha="center", va="bottom", fontsize=7, color=INK2)
    ax.set_xlabel("shortest-path distance d(s, t) in clicks (∞ = no path)")
    ax.set_ylabel("share of random pairs")
    ax.set_title(f"Distance between {p['n']:,} random article pairs")
    ax.grid(axis="x", visible=False)
    save(fig, "fig_distance_distribution", figdir)


def hub_groups(h):
    """Exclusive display groups (a page in several top-K lists goes to the first match)."""
    g = np.select([h.top_out_degree, h.top_in_degree, h.top_path_frequency],
                  ["top-K out-degree", "top-K in-degree", "top-K path frequency only"],
                  default="random baseline")
    return pd.Series(g, index=h.index)


# three hues that stay distinct for colour-blind readers + neutral grey for the baseline
GROUP_STYLE = {
    "top-K out-degree": (BLUE, "o"),
    "top-K in-degree": (ORANGE, "s"),
    "top-K path frequency only": (AQUA, "D"),
    "random baseline": (GREY, "^"),
}


def fig_detour_vs_outdegree(h, res, figdir):
    h = h.assign(group=hub_groups(h))
    fig, ax = plt.subplots(figsize=(6.4, 4.2))
    for grp, (color, marker) in GROUP_STYLE.items():
        x = h[h.group == grp]
        ax.scatter(x.out_degree.clip(lower=1), x.mean_detour, s=18, color=color, marker=marker,
                   edgecolor="white", linewidth=0.5, label=f"{grp} (n={len(x)})", zorder=3)
    # a few direct labels, offset to the left so they never leave the plot
    for _, r in h.nsmallest(3, "mean_detour").iterrows():
        t = nice(r.title)
        ax.annotate(t if len(t) <= 24 else t[:23] + "…", (max(r.out_degree, 1), r.mean_detour),
                    fontsize=6.5, color=INK2, xytext=(-5, 0), textcoords="offset points",
                    ha="right", va="center")
    ax.set_xscale("log")
    ax.set_xlabel("out-degree (perceived 'landmark-ness'), log scale")
    ax.set_ylabel("mean detour (extra clicks via the hub)")
    # The candidate lists were selected on different variables, so a pooled correlation
    # mixes selection effects: the title gives Spearman's rho inside each list instead.
    bg = res["perceived_vs_real"]["spearman_by_group"]
    r = {g: f"{bg[g]['out_degree_vs_mean_detour']['rho']:+.2f}".replace("-", "−") for g in bg}
    ax.set_title("Link-rich pages vs good hubs. Spearman ρ inside each list of 100 (50 random):\n"
                 f"top out-degree {r['top_out_degree']}, top in-degree {r['top_in_degree']}, "
                 f"top path frequency {r['top_path_frequency']}, random {r['random_baseline']}",
                 fontsize=8.5)
    ax.legend(loc="upper right", fontsize=7)
    save(fig, "fig_detour_vs_outdegree", figdir)


def fig_best_hubs(h, figdir, n=20):
    top = h.nsmallest(n, "mean_detour").iloc[::-1]
    fig, ax = plt.subplots(figsize=(6.4, 5.0))
    labels = [f"{nice(t)}  [{ty}]" if isinstance(ty, str) else nice(t)
              for t, ty in zip(top.title, top.get("type", [None] * len(top)))]
    ax.barh(labels, top.mean_detour, color=BLUE, height=0.65)
    for i, (d, onp) in enumerate(zip(top.mean_detour, top.on_path_rate)):
        ax.text(d, i, f" {d:.2f}  (on a shortest path: {100 * onp:.1f}%)", va="center",
                fontsize=6.5, color=INK2)
    ax.set_xlabel("mean detour (extra clicks), lower is better")
    ax.set_title(f"The {n} best hubs among the candidates [type label]")
    ax.grid(axis="y", visible=False)
    ax.set_xlim(0, top.mean_detour.max() * 1.6)
    save(fig, "fig_best_hubs", figdir)


def typed_candidates(h, min_n):
    """Non-random candidates with a label; types with < min_n pages merged (see common)."""
    c = h[~h.random_baseline | h.top_out_degree | h.top_in_degree | h.top_path_frequency].dropna(subset=["type"])
    return c.assign(type=group_small_types(c.type, min_n))


def type_order(c, ascending=True):
    """Types sorted by median mean detour; the merged small types always come last."""
    med = c.groupby("type").mean_detour.median().sort_values(ascending=ascending)
    return [t for t in med.index if t != SMALL_TYPES] + ([SMALL_TYPES] if SMALL_TYPES in med.index else [])


def fig_detour_by_type(h, figdir, min_n):
    if "type" not in h or h.type.isna().all():
        log.warning("no type labels: skipping fig_detour_by_type")
        return
    c = typed_candidates(h, min_n)
    order = type_order(c)
    fig, ax = plt.subplots(figsize=(6.0, 3.6))
    for i, t in enumerate(order):
        y = c[c.type == t].mean_detour.to_numpy()
        jitter = (np.random.default_rng(i).random(len(y)) - 0.5) * 0.35
        ax.scatter(i + jitter, y, s=12, color=BLUE, alpha=0.75, edgecolor="white", linewidth=0.4, zorder=3)
        ax.hlines(np.median(y), i - 0.3, i + 0.3, color=INK, lw=2, zorder=4)
        ax.text(i, -0.02, f"n={len(y)}", ha="center", va="top", fontsize=6.5, color=INK2,
                transform=ax.get_xaxis_transform())
    base = h[h.random_baseline & ~(h.top_out_degree | h.top_in_degree | h.top_path_frequency)]
    ax.axhline(base.mean_detour.median(), color=GREY, lw=1.2, ls="--", zorder=2)
    ax.text(len(order) - 0.5, base.mean_detour.median(), "median of random articles",
            ha="right", va="bottom", fontsize=7, color=INK2)
    names = [f"{t} (n<{min_n} each,\nnot ranked)" if t == SMALL_TYPES else t for t in order]
    ax.set_xticks(range(len(order)), names, rotation=30, ha="right")
    ax.tick_params(axis="x", pad=11)
    ax.set_ylabel("mean detour (extra clicks)")
    ax.set_title(f"Hub quality by page type (candidates; bar = median; types with < {min_n} pages merged)")
    ax.grid(axis="x", visible=False)
    save(fig, "fig_detour_by_type", figdir)


def fig_bias_check(bc, figdir):
    bc = bc[bc.error.isna()]
    fig, ax = plt.subplots(figsize=(5.2, 3.8))
    for grp, color, marker, label in [("top_out_degree", BLUE, "o", "highest out-degree articles"),
                                      ("top_in_degree", ORANGE, "s", "highest in-degree articles"),
                                      ("random", AQUA, "^", "random articles")]:
        x = bc[bc.group == grp]
        ax.scatter(x.html_all.clip(lower=1), x.body.clip(lower=1), s=16, color=color, marker=marker,
                   edgecolor="white", linewidth=0.5, label=f"{label} (n={len(x)})", zorder=3)
    lim = [1, max(bc.html_all.max(), 10) * 1.3]
    ax.plot(lim, lim, color=GREY, lw=1, ls="--")
    ax.text(2, 1.3, "on the dashed line: no navigation-box links", fontsize=7, color=INK2, ha="left")
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlabel("distinct article links in the rendered page")
    ax.set_ylabel("…after removing navigation boxes & portals")
    ax.set_title("How much of the out-degree comes from navigation templates?")
    ax.legend(loc="upper left", fontsize=7)
    save(fig, "fig_bias_check", figdir)


def main() -> None:
    cfg = load_config()
    A = cfg["analysis"]
    figdir = path(cfg, "figures")
    gen = path(cfg, "generated")
    sample = gen / f"sample_seed{A['seed']}_n{A['n_pairs']}"
    res = read_json(sample / "results.json")
    hubs = pd.read_csv(sample / "hubs.csv")
    lab_p = ROOT / "labels" / "hub_type_labels.csv"
    if lab_p.exists():
        hubs = hubs.merge(pd.read_csv(lab_p)[["page_id", "type"]], on="page_id", how="left")
    counts = load_degree_counts(cfg)
    if counts is not None:
        fig_degree_ccdf(counts, figdir)
    fig_distance_distribution(res, figdir)
    fig_detour_vs_outdegree(hubs, res, figdir)
    fig_best_hubs(hubs, figdir)
    fig_detour_by_type(hubs, figdir, A["min_type_size"])
    if (gen / "bias_check.csv").exists():
        fig_bias_check(pd.read_csv(gen / "bias_check.csv"), figdir)
    from . import slide_figures  # figures sized for the 16:9 slides
    slide_figures.main()


if __name__ == "__main__":
    main()
