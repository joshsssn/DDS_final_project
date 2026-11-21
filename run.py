"""Single entry point: reproduce the whole project, step by step.

    uv run python run.py all            # everything, from download to report numbers
    uv run python run.py <step> [...]   # one or several steps
    uv run python run.py all --force    # redo steps even if their outputs exist

Steps (in order): download, parse, clean, store, analyze, robustness, bias, redirects, figures,
numbers, notebook, test. The expensive steps (parse, clean, store) are skipped when their
complete output exists. `download` verifies checksums itself and skips valid files.
The other steps always run, because they are cheap or cached: `analyze` reuses the
persistent BFS cache in data/processed/cache (wiped automatically if the graph
changes), and `bias` reuses the API cache in data/raw/api. So figures, numbers and the
notebook always reflect the current config and labels. To evaluate another sample:
uv run python -m wikigame.analysis --seed 43 --n-pairs 2000
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
CFG = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))

def parse_done() -> bool:
    p = ROOT / CFG["paths"]["interim"] / "parse_stats.json"
    if not p.exists():
        return False
    stats = json.loads(p.read_text(encoding="utf-8"))
    return all(t in stats for t in CFG["dump"]["tables"])  # a partial parse is not done


def clean_done() -> bool:
    g = ROOT / CFG["paths"]["processed"] / "graph"
    return (ROOT / CFG["paths"]["processed"] / "quality.json").exists() and all(
        (g / f).exists() for f in ["nodes.parquet", "fwd_indptr.npy", "fwd_indices.npy",
                                   "rev_indptr.npy", "rev_indices.npy"])


def exists(p: Path):
    return lambda: p.exists()


# step -> (command, "is done?" check, or None = always run)
STEPS = {
    "download": (["-m", "wikigame.download"], None),
    "parse": (["-m", "wikigame.parse_sql"], parse_done),
    "clean": (["-m", "wikigame.clean"], clean_done),
    "store": (["-m", "wikigame.storage_benchmark"],
              exists(ROOT / CFG["paths"]["generated"] / "storage_benchmark.json")),
    "analyze": (["-m", "wikigame.analysis"], None),
    "robustness": (["-m", "wikigame.analysis", "--seed", str(CFG["analysis"]["robustness_seed"])], None),
    "bias": (["-m", "wikigame.bias_check"], None),
    # needs data/interim (parse) and the graph (clean); skipped when its output exists
    "redirects": (["-m", "wikigame.redirect_share"],
                  exists(ROOT / CFG["paths"]["generated"] / "redirect_share.json")),
    "figures": (["-m", "wikigame.figures"], None),
    "numbers": (["-m", "wikigame.report_numbers"], None),
    "notebook": (["-m", "jupyter", "execute", "--inplace", "notebooks/final_analysis.ipynb"], None),
    "test": (["-m", "pytest", "-q"], None),
}


def run(step: str, force: bool) -> None:
    cmd, done = STEPS[step]
    if done is not None and done() and not force:
        print(f"[run] {step}: complete output exists, skipping (use --force to redo)")
        return
    print(f"[run] {step}: {' '.join(cmd)}")
    t0 = time.perf_counter()
    env = {**os.environ, "PYTHONUTF8": "1"}
    subprocess.run([sys.executable, *cmd], cwd=ROOT, env=env, check=True)
    print(f"[run] {step}: done in {time.perf_counter() - t0:.0f} s")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("steps", nargs="+", choices=[*STEPS, "all"])
    ap.add_argument("--force", action="store_true", help="redo steps whose output exists")
    a = ap.parse_args()
    steps = list(STEPS) if "all" in a.steps else a.steps
    for s in steps:
        run(s, a.force)


if __name__ == "__main__":
    main()
