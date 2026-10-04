"""Bonus - interactive shortest-path explorer for the French Wikipedia link graph.

    uv run python bonus/server.py          then open http://localhost:8000
    (or double-click Bonus.bat at the root of the repository on Windows)

A tiny local web server (standard library only) on top of the cleaned graph:
  GET /                      the page (bonus/index.html)
  GET /api/search?q=fra      article search: titles and redirect titles, accent- and
                             case-insensitive; prefix matches first, then words inside a
                             title, then close spellings; most-linked pages first
  GET /api/suggest           a few well-linked articles, as ideas
  GET /api/random            a random pair of articles
  GET /api/path?s=..&t=..    bidirectional BFS between two titles: distance, a few
                             shortest paths, and a small sample of each explored level
                             so that the page can replay the two searches meeting.

The search is the same bidirectional BFS as in the analysis (graph._expand); here we
also keep every explored level, to replay the search in the browser.
The first start builds a search index over the 2.78 M article titles and the 1.84 M
redirect titles (about 30 s) and caches it in data/processed/bonus_index/; later
starts take a few seconds.
"""
from __future__ import annotations

import bisect
import difflib
import json
import sys
import time
import unicodedata
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from wikigame.common import load_config, path  # noqa: E402
from wikigame.graph import UNREACHABLE, Graph, _expand  # noqa: E402

U = int(UNREACHABLE)
SAMPLE_PER_LEVEL = 26   # explored pages drawn per level and per side
MAX_PATHS = 6           # distinct shortest paths returned


_EXTRA = str.maketrans({"œ": "oe", "æ": "ae", "’": "'", "ʼ": "'", "_": " ", "–": "-", "—": "-"})


def fold(text: str) -> str:
    """Search key of a title: no accents, no case, spaces instead of underscores."""
    text = unicodedata.normalize("NFKD", text.translate(_EXTRA).casefold())
    return "".join(c for c in text if not unicodedata.combining(c)).translate(_EXTRA).strip()


class Explorer:
    def __init__(self):
        cfg = load_config()
        t0 = time.perf_counter()
        proc = path(cfg, "processed")
        self.g = Graph.load(proc / "graph")
        nodes = self.g.nodes
        self.titles = nodes["title"].tolist()
        self.indeg = nodes["in_degree"].to_numpy()
        self.eligible = np.flatnonzero(~nodes["is_disambig"].to_numpy())
        self.notable = np.flatnonzero((self.indeg >= 2500) & (nodes["out_degree"].to_numpy() >= 150)
                                      & ~nodes["is_disambig"].to_numpy())
        self._load_index(cfg, proc / "bonus_index")
        self.rng = np.random.default_rng()
        print(f"ready: {self.g.fwd.n:,} articles, {len(self.keys):,} searchable titles, "
              f"loaded in {time.perf_counter() - t0:.1f} s", flush=True)

    # ---- search index -----------------------------------------------------------------
    def _load_index(self, cfg, folder: Path) -> None:
        """Sorted search keys over article titles AND redirect titles.
        keys[i] = folded title, names[i] = title as written, node[i] = article it leads to,
        redirect[i] = whether names[i] is a redirect title."""
        if not (folder / "node.npy").exists():
            print("building the search index (first start only, about 30 s)...", flush=True)
            nodes = self.g.nodes.reset_index()[["node_id", "page_id", "title"]]
            interim = path(cfg, "interim")
            pages = pd.read_parquet(interim / "page.parquet", columns=["page_id", "page_namespace", "page_title", "page_is_redirect"])
            pages = pages[(pages.page_namespace == 0) & pages.page_is_redirect][["page_id", "page_title"]]
            res = pd.read_parquet(path(cfg, "processed") / "redirect_resolution.parquet")
            res = res[res.status == "resolved"][["page_id", "final_page_id"]]
            red = pages.merge(res, on="page_id").merge(
                nodes.rename(columns={"page_id": "final_page_id"})[["final_page_id", "node_id"]], on="final_page_id")
            names = nodes.title.tolist() + red.page_title.tolist()
            node = np.concatenate([nodes.node_id.to_numpy(), red.node_id.to_numpy()]).astype(np.int32)
            redirect = np.concatenate([np.zeros(len(nodes), bool), np.ones(len(red), bool)])
            keys = [fold(t) for t in names]
            order = sorted(range(len(keys)), key=keys.__getitem__)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "keys.txt").write_text("\n".join(keys[i] for i in order), encoding="utf-8")
            (folder / "names.txt").write_text("\n".join(names[i] for i in order), encoding="utf-8")
            np.save(folder / "redirect.npy", redirect[order])
            np.save(folder / "node.npy", node[order])
        self.keys = (folder / "keys.txt").read_text(encoding="utf-8").split("\n")
        self.names = (folder / "names.txt").read_text(encoding="utf-8").split("\n")
        self.node = np.load(folder / "node.npy")
        self.redirect = np.load(folder / "redirect.npy")
        self.key_deg = self.indeg[self.node]
        # one long text of all keys, to find a word in the middle of a title quickly
        self.text = "\n".join(self.keys)
        lengths = np.fromiter(map(len, self.keys), dtype=np.int64, count=len(self.keys))
        self.starts = np.concatenate([[0], np.cumsum(lengths + 1)[:-1]])

    def label(self, i: int) -> str:
        return self.titles[i].replace("_", " ")

    def _prefix(self, key: str, limit: int = 60) -> np.ndarray:
        """Index entries whose key starts with `key`, most-linked first."""
        lo = bisect.bisect_left(self.keys, key)
        hi = bisect.bisect_left(self.keys, key + "￿")
        if hi <= lo:
            return np.empty(0, dtype=np.int64)
        deg = self.key_deg[lo:hi]
        k = min(limit, hi - lo)
        top = np.argpartition(-deg, k - 1)[:k]
        return lo + top[np.argsort(-deg[top], kind="stable")]

    def _words(self, key: str, limit: int = 300) -> np.ndarray:
        """Index entries with `key` at the start of a word inside the title."""
        hits, pos, tries = [], self.text.find(key), 0
        while pos != -1 and len(hits) < limit and tries < 4000:
            if pos and self.text[pos - 1] in " (-'/":
                hits.append(pos)
            pos = self.text.find(key, pos + 1)
            tries += 1
        if not hits:
            return np.empty(0, dtype=np.int64)
        idx = np.unique(np.searchsorted(self.starts, hits, side="right") - 1)
        return idx[np.argsort(-self.key_deg[idx], kind="stable")]

    def _close(self, key: str) -> list[int]:
        """Entries whose spelling is close to `key`: one letter dropped, two letters swapped,
        or the ending mistyped. Ranked by similarity, then by links."""
        variants = {key[:i] + key[i + 1:] for i in range(1, len(key))}
        variants |= {key[:i] + key[i + 1] + key[i] + key[i + 2:] for i in range(len(key) - 1)}
        variants |= {key[:-1], key[:-2]} if len(key) > 4 else {key[:-1]}
        cand = set()
        for v in variants:
            if len(v) >= 3:
                cand.update(self._prefix(v, 12).tolist())
        scored = []
        for i in cand:
            ratio = difflib.SequenceMatcher(None, key, self.keys[i][:len(key) + 2]).ratio()
            if ratio >= 0.72:
                scored.append((-round(ratio, 2), -int(self.key_deg[i]), i))
        return [i for _, _, i in sorted(scored)]

    def search(self, q: str, k: int = 7) -> list[dict]:
        key = fold(q)
        if not key:
            return []
        out, seen = [], set()

        def take(indices, kind):
            for i in indices:
                node = int(self.node[i])
                if node in seen or len(out) >= k:
                    continue
                seen.add(node)
                exact = self.keys[i] == key
                out.append({"title": self.label(node), "links_in": int(self.indeg[node]),
                            "via": self.names[i].replace("_", " ") if self.redirect[i] else None,
                            "kind": "exact" if exact else kind})

        lo = bisect.bisect_left(self.keys, key)
        hi = bisect.bisect_right(self.keys, key)
        exact = sorted(range(lo, hi), key=lambda i: (self.redirect[i], -self.key_deg[i]))
        take(exact, "exact")
        take(self._prefix(key), "prefix")
        if len(out) < k and len(key) >= 3:
            take(self._words(key), "word")
        if len(out) < k and len(key) >= 4:
            take(self._close(key), "close")
        return out

    def resolve(self, q: str) -> int | None:
        """Article for an exact title or redirect title (accents and case ignored)."""
        key = fold(q)
        lo = bisect.bisect_left(self.keys, key)
        hi = bisect.bisect_right(self.keys, key)
        if hi <= lo:
            return None
        best = min(range(lo, hi), key=lambda i: (self.redirect[i], -self.key_deg[i]))
        return int(self.node[best])

    def suggest(self, k: int = 6) -> list[dict]:
        picks = self.rng.choice(self.notable, size=k, replace=False)
        return [{"title": self.label(int(i)), "links_in": int(self.indeg[i]), "via": None, "kind": "idea"} for i in picks]

    def random_pair(self) -> dict:
        s, t = self.rng.choice(self.eligible, size=2, replace=False)
        return {"s": self.label(int(s)), "t": self.label(int(t))}

    # ---- search ----------------------------------------------------------------------
    def find(self, s: int, t: int) -> dict:
        g, rng = self.g, self.rng
        t0 = time.perf_counter()
        df = np.full(g.fwd.n, UNREACHABLE, dtype=np.uint8)
        db = np.full(g.fwd.n, UNREACHABLE, dtype=np.uint8)
        df[s], db[t] = 0, 0
        lev_f = [np.array([s], dtype=np.int64)]      # every explored level, per side
        lev_b = [np.array([t], dtype=np.int64)]
        steps, d, met = [], (0 if s == t else U), np.empty(0, dtype=np.int64)
        while d == U and lev_f[-1].size and lev_b[-1].size:
            ff, fb = lev_f[-1], lev_b[-1]
            cost_f = int((g.fwd.indptr[ff + 1] - g.fwd.indptr[ff]).sum())
            cost_b = int((g.rev.indptr[fb + 1] - g.rev.indptr[fb]).sum())
            if cost_f <= cost_b:
                new = _expand(g.fwd, df, ff, len(lev_f))
                lev_f.append(new)
                side, level, other = "f", len(lev_f) - 1, db
            else:
                new = _expand(g.rev, db, fb, len(lev_b))
                lev_b.append(new)
                side, level, other = "b", len(lev_b) - 1, df
            steps.append({"side": side, "level": level, "size": int(new.size)})
            hit = new[other[new] != UNREACHABLE]
            if hit.size:
                total = df[hit].astype(np.int64) + db[hit].astype(np.int64)
                d = int(total.min())
                met = hit[total == d]
        search_ms = (time.perf_counter() - t0) * 1000
        if not lev_f[-1].size:
            lev_f.pop()
        if not lev_b[-1].size:
            lev_b.pop()
        lf, lb = len(lev_f) - 1, len(lev_b) - 1

        shown: dict[int, dict] = {}
        edges: set[tuple[int, int]] = set()

        def add(node, col, side, parent=None):
            cur = shown.get(node)
            if cur is None:
                shown[node] = {"col": col, "side": side, "path": False, "parent": parent}
            elif cur["side"] != side:
                cur["side"] = "m"   # labelled by both searches

        def back_to_s(node):
            """Chain s -> ... -> node through forward levels (random predecessors)."""
            chain = [int(node)]
            for level in range(int(df[node]) - 1, -1, -1):
                p = g.rev.neighbours(chain[-1])
                chain.append(int(rng.choice(p[df[p] == level])))
            return chain[::-1]

        def on_to_t(node):
            chain = [int(node)]
            for level in range(int(db[node]) - 1, -1, -1):
                q = g.fwd.neighbours(chain[-1])
                chain.append(int(rng.choice(q[db[q] == level])))
            return chain

        reachable = d != U
        width = max(d, 1) if reachable else lf + lb + 1     # index of the last column
        add(s, 0, "f")
        add(t, width, "b")
        for level in range(1, lf + 1):
            pool = lev_f[level]
            for n in rng.choice(pool, size=min(SAMPLE_PER_LEVEL, pool.size), replace=False):
                chain = back_to_s(n)
                for i in range(1, len(chain)):
                    add(chain[i], i, "f", chain[i - 1])
                edges.update(zip(chain, chain[1:]))
        for level in range(1, lb + 1):
            pool = lev_b[level]
            for n in rng.choice(pool, size=min(SAMPLE_PER_LEVEL, pool.size), replace=False):
                chain = on_to_t(n)
                for i in range(len(chain) - 1):
                    add(chain[i], width - (level - i), "b", chain[i + 1])
                edges.update(zip(chain, chain[1:]))

        paths = []
        if reachable and d > 0:
            seen = set()
            for _ in range(MAX_PATHS * 6):
                m = int(rng.choice(met))
                p = tuple(back_to_s(m) + on_to_t(m)[1:])
                if p not in seen:
                    seen.add(p)
                    paths.append(list(p))
                    for i, x in enumerate(p):
                        if i <= lf:
                            add(x, i, "f", p[i - 1] if i else None)
                        else:
                            add(x, i, "b", p[i + 1] if i + 1 < len(p) else None)
                        shown[x]["path"] = True
                    edges.update(zip(p, p[1:]))
                if len(paths) == MAX_PATHS:
                    break
        elif d == 0:
            paths = [[s]]
            shown[s]["path"] = True

        return {
            "distance": d if reachable else None, "start": s, "target": t, "width": width,
            "search_ms": round(search_ms, 1), "total_ms": round((time.perf_counter() - t0) * 1000, 1),
            "explored_start": int(sum(x.size for x in lev_f)), "explored_target": int(sum(x.size for x in lev_b)),
            "articles": int(g.fwd.n), "meeting_pages": int(met.size), "levels_start": lf, "levels_target": lb,
            "steps": steps, "paths": paths, "edges": [list(e) for e in edges],
            "nodes": {str(k): {**v, "title": self.label(k), "raw": self.titles[k]} for k, v in shown.items()},
        }


class Handler(BaseHTTPRequestHandler):
    ex: Explorer = None

    def log_message(self, *args):   # quiet
        pass

    def send(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, code=200):
        self.send(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", code)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        ex = self.ex
        if url.path in ("/", "/index.html"):
            return self.send((Path(__file__).parent / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/search":
            return self.json(ex.search(q.get("q", "")))
        if url.path == "/api/suggest":
            return self.json(ex.suggest())
        if url.path == "/api/random":
            return self.json(ex.random_pair())
        if url.path == "/api/path":
            s, t = ex.resolve(q.get("s", "")), ex.resolve(q.get("t", ""))
            if s is None or t is None:
                which = "s" if s is None else "t"
                return self.json({"error": f"There is no article named “{q.get(which, '')}”.", "field": which,
                                  "suggestions": ex.search(q.get(which, ""))}, 404)
            return self.json(ex.find(s, t))
        self.json({"error": "not found"}, 404)


def main(port: int = 8000, open_browser: bool = False) -> None:
    url = f"http://localhost:{port}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)   # fails fast if already running
    except OSError:
        print(f"port {port} is busy: the explorer is probably already running at {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        return
    Handler.ex = Explorer()
    print(f"open {url}   (Ctrl+C or close this window to stop)", flush=True)
    if open_browser:
        webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--open"]
    main(int(args[0]) if args else 8000, open_browser="--open" in sys.argv)
