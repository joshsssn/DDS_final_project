"""Shared helpers: configuration, paths, logging, small JSON utilities."""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_config(path: Path | None = None) -> dict:
    # WIKIGAME_CONFIG lets tests/dry runs point to another config (e.g. a scratch cache)
    path = path or os.environ.get("WIKIGAME_CONFIG") or ROOT / "config.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def path(cfg: dict, key: str) -> Path:
    """Absolute path of a configured directory (created if missing)."""
    p = ROOT / cfg["paths"][key]
    p.mkdir(parents=True, exist_ok=True)
    return p


def dump_filename(cfg: dict, table: str) -> str:
    d = cfg["dump"]
    return f"{d['wiki']}-{d['date']}-{table}.sql.gz"


def get_logger(name: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    return logging.getLogger(name)


@contextmanager
def timed(log: logging.Logger, what: str, record: dict | None = None):
    """Log the wall-clock duration of a block; optionally store it in `record`."""
    t0 = time.perf_counter()
    log.info("start: %s", what)
    yield
    dt = time.perf_counter() - t0
    log.info("done: %s in %.1f s", what, dt)
    if record is not None:
        record[what] = round(dt, 2)


def write_json(obj, p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, default=_default)


def read_json(p: Path):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _default(o):
    # numpy scalars -> python scalars
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(type(o))
