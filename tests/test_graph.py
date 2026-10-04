"""BFS and CSR tests on small toy graphs, checked against a naive reference BFS."""
from collections import deque

import numpy as np
import pytest

from wikigame import graph as G

U = int(G.UNREACHABLE)


def make(edges, n):
    e = np.array(edges, dtype=np.int64).reshape(-1, 2)
    fwd = G.csr_from_edges(e[:, 0], e[:, 1], n)
    rev = G.csr_from_edges(e[:, 1], e[:, 0], n)
    return fwd, rev


def naive_bfs(edges, n, s):
    adj = {u: [] for u in range(n)}
    for a, b in edges:
        adj[a].append(b)
    dist = [U] * n
    dist[s] = 0
    q = deque([s])
    while q:
        u = q.popleft()
        for v in adj[u]:
            if dist[v] == U:
                dist[v] = dist[u] + 1
                q.append(v)
    return dist


def test_csr_structure():
    fwd, rev = make([(0, 2), (0, 1), (2, 0)], 4)
    assert list(fwd.indptr) == [0, 2, 2, 3, 3]
    assert list(fwd.neighbours(0)) == [1, 2]  # sorted
    assert list(rev.neighbours(0)) == [2]
    assert list(fwd.degree()) == [2, 0, 1, 0]


def test_directed_chain_and_unreachable():
    edges = [(0, 1), (1, 2), (2, 3)]
    fwd, rev = make(edges, 5)
    assert list(G.bfs(fwd, 0)) == [0, 1, 2, 3, U]
    assert list(G.bfs(fwd, 3)) == [U, U, U, 0, U]      # direction matters
    assert list(G.bfs(rev, 3)) == [3, 2, 1, 0, U]      # reverse BFS = distances *to* 3


def test_cycle_and_self_loop():
    fwd, _ = make([(0, 1), (1, 2), (2, 0), (1, 1)], 3)
    assert list(G.bfs(fwd, 1)) == [2, 0, 1]


def test_random_graphs_match_naive():
    rng = np.random.default_rng(0)
    for _ in range(30):
        n = int(rng.integers(2, 40))
        m = int(rng.integers(0, 4 * n))
        edges = {(int(a), int(b)) for a, b in rng.integers(0, n, size=(m, 2))}
        fwd, _ = make(sorted(edges), n) if edges else (G.CSR(np.zeros(n + 1, np.int64),
                                                             np.zeros(0, np.int32)), None)
        for s in range(n):
            assert list(G.bfs(fwd, s)) == naive_bfs(edges, n, s)


def test_chunked_gather(monkeypatch):
    # force tiny chunks so the chunking logic is exercised
    monkeypatch.setattr(G, "_EDGE_CHUNK", 3)
    edges = [(0, i) for i in range(1, 10)] + [(i, i + 10) for i in range(1, 10)]
    fwd, _ = make(edges, 20)
    assert list(G.bfs(fwd, 0)) == naive_bfs(edges, 20, 0)


def test_early_exit_target_distance_is_exact():
    edges = [(0, 1), (1, 2), (2, 3), (0, 4), (4, 3)]
    fwd, _ = make(edges, 5)
    assert G.bfs(fwd, 0, target=3)[3] == 2


def test_sample_shortest_path_is_shortest_and_valid():
    # two tied shortest paths 0->1->3 and 0->2->3, plus a longer one
    edges = [(0, 1), (0, 2), (1, 3), (2, 3), (0, 4), (4, 5), (5, 3)]
    fwd, rev = make(edges, 6)
    dist = G.bfs(fwd, 0)
    rng = np.random.default_rng(1)
    seen = set()
    for _ in range(50):
        p = G.sample_shortest_path(rev, dist, 3, rng)
        assert p[0] == 0 and p[-1] == 3 and len(p) == 3
        assert all((a, b) in set(edges) for a, b in zip(p, p[1:]))
        seen.add(p[1])
    assert seen == {1, 2}  # both tied paths are sampled


def test_unreachable_path_raises():
    fwd, rev = make([(0, 1)], 3)
    with pytest.raises(ValueError):
        G.sample_shortest_path(rev, G.bfs(fwd, 0), 2, np.random.default_rng(0))


def test_bidirectional_matches_bfs_and_paths_are_valid(monkeypatch):
    monkeypatch.setattr(G, "_EDGE_CHUNK", 5)
    rng = np.random.default_rng(3)
    for _ in range(40):
        n = int(rng.integers(2, 30))
        m = int(rng.integers(0, 4 * n))
        edges = sorted({(int(a), int(b)) for a, b in rng.integers(0, n, size=(m, 2))})
        if not edges:
            continue
        fwd, rev = make(edges, n)
        eset = set(edges)
        for s in range(n):
            ref = naive_bfs(edges, n, s)
            for t in range(n):
                d, p = G.bidirectional_distance(fwd, rev, s, t, rng)
                assert d == ref[t]
                assert G.bidirectional_distance(fwd, rev, s, t)[0] == ref[t]
                if d == U:
                    assert p == []
                else:
                    assert p[0] == s and p[-1] == t and len(p) == d + 1
                    assert all((a, b) in eset for a, b in zip(p, p[1:]))
