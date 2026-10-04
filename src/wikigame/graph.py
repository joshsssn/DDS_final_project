"""Compact integer-indexed directed graph (CSR arrays) and breadth-first search.

Storage on disk (data/processed/graph/):
    nodes.parquet           node_id, page_id, title, is_disambig, out_degree, in_degree
    fwd_indptr.npy (int64)  CSR row pointers of the forward graph (article -> linked article)
    fwd_indices.npy (int32) concatenated, sorted out-neighbour lists
    rev_indptr.npy / rev_indices.npy   same for the reverse graph (in-neighbours)

The out-neighbours of node u are indices[indptr[u]:indptr[u+1]].
Arrays are loaded with mmap_mode="r": several BFS worker processes then share the
same physical memory through the OS page cache.

Distances are stored as uint8; UNREACHABLE = 255 encodes d = infinity. A BFS that
would need a level >= 255 raises an error rather than wrapping around.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

UNREACHABLE = np.uint8(255)
_EDGE_CHUNK = 20_000_000  # max edges gathered at once (bounds peak memory per level)


@dataclass
class CSR:
    indptr: np.ndarray
    indices: np.ndarray

    @property
    def n(self) -> int:
        return len(self.indptr) - 1

    def degree(self) -> np.ndarray:
        return np.diff(self.indptr)

    def neighbours(self, u: int) -> np.ndarray:
        return self.indices[self.indptr[u]:self.indptr[u + 1]]


def csr_from_edges(src: np.ndarray, dst: np.ndarray, n: int) -> CSR:
    """Build a CSR from an edge list (duplicates must already be removed).
    Neighbour lists are sorted, so the result does not depend on input order."""
    # one stable sort on a combined (src, dst) 64-bit key: faster than lexsort
    key = (np.asarray(src, dtype=np.int64) << 32) | np.asarray(dst, dtype=np.int64)
    order = np.argsort(key, kind="stable")
    del key
    indices = np.asarray(dst)[order].astype(np.int32)
    counts = np.bincount(src, minlength=n)
    indptr = np.zeros(n + 1, dtype=np.int64)
    np.cumsum(counts, out=indptr[1:])
    return CSR(indptr, indices)


def _gather(g: CSR, frontier: np.ndarray):
    """Yield the concatenated out-neighbours of `frontier`, in bounded chunks."""
    starts = g.indptr[frontier]
    lens = g.indptr[frontier + 1] - starts
    cum = np.cumsum(lens)
    i = 0
    while i < len(frontier):
        # take as many frontier nodes as fit in one chunk (at least one)
        base = cum[i - 1] if i else 0
        j = max(int(np.searchsorted(cum, base + _EDGE_CHUNK, side="right")), i + 1)
        s, ln = starts[i:j], lens[i:j]
        total = int(ln.sum())
        if total:
            # positions s[k], s[k]+1, ..., s[k]+ln[k]-1 for every k, without a Python loop
            offsets = np.repeat(s - np.cumsum(ln) + ln, ln) + np.arange(total, dtype=np.int64)
            yield g.indices[offsets]
        i = j


def bfs(g: CSR, source: int, target: int | None = None) -> np.ndarray:
    """Level-synchronous BFS from `source`.

    Returns dist (uint8, UNREACHABLE where not reached). If `target` is given the search
    stops as soon as the target's level is complete (early exit); nodes farther than the
    target are then left UNREACHABLE and must not be used.
    """
    dist = np.full(g.n, UNREACHABLE, dtype=np.uint8)
    dist[source] = 0
    frontier = np.array([source], dtype=np.int64)
    level = 0
    while frontier.size:
        if target is not None and dist[target] != UNREACHABLE:
            break
        level += 1
        if level >= int(UNREACHABLE):
            raise OverflowError("BFS depth >= 255 does not fit in uint8 distances")
        found = False
        for nbrs in _gather(g, frontier):
            new = nbrs[dist[nbrs] == UNREACHABLE]
            if new.size:
                dist[new] = level  # duplicates in `new` are harmless
                found = True
        if not found:
            break
        frontier = np.flatnonzero(dist == level)
    return dist


def sample_shortest_path(rev: CSR, dist_from_s: np.ndarray, t: int,
                         rng: np.random.Generator) -> list[int]:
    """Walk back from t to s along BFS levels, choosing a random predecessor at each
    step. Returns the path [s, ..., t]. Requires dist_from_s[t] < UNREACHABLE.

    Note: choosing uniformly among predecessors at each step is NOT uniform over all
    shortest paths; it is only used to discover candidate hubs (documented)."""
    d = int(dist_from_s[t])
    if d == UNREACHABLE:
        raise ValueError("t not reachable from s")
    path = [t]
    cur = t
    for level in range(d - 1, -1, -1):
        preds = rev.neighbours(cur)
        preds = preds[dist_from_s[preds] == level]
        cur = int(rng.choice(preds))
        path.append(cur)
    return path[::-1]


def _expand(g: CSR, dist: np.ndarray, frontier: np.ndarray, level: int) -> np.ndarray:
    """Expand one full BFS level: label unvisited neighbours with `level`, return them."""
    found = []
    for nbrs in _gather(g, frontier):
        new = nbrs[dist[nbrs] == UNREACHABLE]
        if new.size:
            dist[new] = level
            found.append(new)
    return np.unique(np.concatenate(found)) if found else np.empty(0, dtype=np.int64)


def bidirectional_distance(fwd: CSR, rev: CSR, s: int, t: int,
                           rng: np.random.Generator | None = None) -> tuple[int, list[int]]:
    """Exact d(s, t) by bidirectional BFS, plus one sampled shortest path if `rng` is given.

    A forward search from s (out-links) and a backward search from t (in-links) each grow
    by whole levels; at every step the side with fewer edges to scan is expanded. Once the
    forward search has completed level a and the backward search level b, any shortest path
    of length <= a + b has a node labelled exactly by both searches, so the first time a
    newly labelled node is also labelled by the other side, the minimum of
    dist_f + dist_b over those nodes is exactly d(s, t).
    Far fewer edges are scanned than by a one-sided BFS (two half-depth balls instead of
    one full-depth ball). Returns (UNREACHABLE, []) when there is no path.

    Path sampling: a random meeting node among the minimisers, then random predecessors
    back to s and random successors on to t. Not uniform over shortest paths; only used
    to discover candidate hubs."""
    if s == t:
        return 0, [s]
    df = np.full(fwd.n, UNREACHABLE, dtype=np.uint8)
    db = np.full(fwd.n, UNREACHABLE, dtype=np.uint8)
    df[s], db[t] = 0, 0
    ff = np.array([s], dtype=np.int64)
    fb = np.array([t], dtype=np.int64)
    lf = lb = 0
    while ff.size and fb.size:
        cost_f = int((fwd.indptr[ff + 1] - fwd.indptr[ff]).sum())
        cost_b = int((rev.indptr[fb + 1] - rev.indptr[fb]).sum())
        if lf + lb + 1 >= int(UNREACHABLE):
            raise OverflowError("distance >= 255 does not fit in uint8")
        if cost_f <= cost_b:
            lf += 1
            ff = new = _expand(fwd, df, ff, lf)
            other = db
        else:
            lb += 1
            fb = new = _expand(rev, db, fb, lb)
            other = df
        met = new[other[new] != UNREACHABLE]
        if met.size:
            total = df[met].astype(np.int64) + db[met].astype(np.int64)
            d = int(total.min())
            if rng is None:
                return d, []
            m = int(rng.choice(met[total == d]))
            left = [m]                      # walk back to s through forward levels
            for level in range(int(df[m]) - 1, -1, -1):
                p = rev.neighbours(left[-1])
                left.append(int(rng.choice(p[df[p] == level])))
            right = []                      # walk on to t through backward levels
            cur = m
            for level in range(int(db[m]) - 1, -1, -1):
                q = fwd.neighbours(cur)
                cur = int(rng.choice(q[db[q] == level]))
                right.append(cur)
            return d, left[::-1] + right
    return int(UNREACHABLE), []


# ---- persistence ----------------------------------------------------------------

def save_csr(g: CSR, folder: Path, prefix: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    np.save(folder / f"{prefix}_indptr.npy", g.indptr)
    np.save(folder / f"{prefix}_indices.npy", g.indices)


def load_csr(folder: Path | str, prefix: str, mmap: bool = True) -> CSR:
    folder = Path(folder)
    mode = "r" if mmap else None
    return CSR(np.load(folder / f"{prefix}_indptr.npy", mmap_mode=mode),
               np.load(folder / f"{prefix}_indices.npy", mmap_mode=mode))


def graph_fingerprint(folder: Path | str) -> dict:
    """Identity of a built graph: sizes + SHA-1 of the forward CSR row pointers
    (which change if any node or any out-degree changes)."""
    import hashlib
    folder = Path(folder)
    indptr = np.load(folder / "fwd_indptr.npy", mmap_mode="r")
    h = hashlib.sha1(np.ascontiguousarray(indptr).tobytes())
    h.update(np.load(folder / "rev_indptr.npy", mmap_mode="r").tobytes())
    return {"nodes": int(len(indptr) - 1), "edges": int(indptr[-1]), "indptr_sha1": h.hexdigest()}


@dataclass
class Graph:
    fwd: CSR
    rev: CSR
    nodes: pd.DataFrame  # indexed by node_id

    @classmethod
    def load(cls, folder: Path | str, mmap: bool = True, with_titles: bool = True) -> "Graph":
        folder = Path(folder)
        cols = None if with_titles else ["node_id", "page_id", "is_disambig",
                                         "out_degree", "in_degree"]
        nodes = pd.read_parquet(folder / "nodes.parquet", columns=cols).set_index("node_id")
        return cls(load_csr(folder, "fwd", mmap), load_csr(folder, "rev", mmap), nodes)

    def node_of(self, title: str) -> int:
        hit = self.nodes.index[self.nodes["title"] == title.replace(" ", "_")]
        if len(hit) != 1:
            raise KeyError(title)
        return int(hit[0])
