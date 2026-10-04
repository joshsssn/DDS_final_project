"""Phase 1a - download the pinned French Wikipedia dump files.

* descriptive User-Agent (config.yaml),
* resume support (HTTP Range) for interrupted downloads,
* SHA-1 verification against the official checksum file of the dump,
* files already present and valid are skipped.

A manifest (data/raw/manifest.json) records URL, size, SHA-1 and acquisition time
of every file.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import sys

import requests

from .common import dump_filename, get_logger, load_config, path, read_json, write_json

log = get_logger("download")
CHUNK = 1 << 20


def sha1_of(p) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(CHUNK * 8), b""):
            h.update(block)
    return h.hexdigest()


def fetch_checksums(cfg, session) -> dict[str, str]:
    d = cfg["dump"]
    name = f"{d['wiki']}-{d['date']}-sha1sums.txt"
    url = f"{d['base_url']}/{d['wiki']}/{d['date']}/{name}"
    target = path(cfg, "raw") / name
    if not target.exists():
        r = session.get(url, timeout=60)
        r.raise_for_status()
        target.write_bytes(r.content)
    sums = {}
    for line in target.read_text(encoding="utf-8").splitlines():
        digest, fname = line.split()
        sums[fname] = digest
    return sums


def check_dump_status(cfg, session) -> None:
    """Save dumpstatus.json next to the raw files and require that the dump is complete:
    every job of the dump must be 'done' (this is what "complete dump" means)."""
    d = cfg["dump"]
    target = path(cfg, "raw") / f"{d['wiki']}-{d['date']}-dumpstatus.json"
    if not target.exists():
        r = session.get(f"{d['base_url']}/{d['wiki']}/{d['date']}/dumpstatus.json", timeout=60)
        r.raise_for_status()
        target.write_bytes(r.content)
    jobs = read_json(target)["jobs"]
    not_done = sorted(k for k, v in jobs.items() if v.get("status") != "done")
    log.info("dumpstatus: %d jobs, %d not done", len(jobs), len(not_done))
    if not_done:
        sys.exit(f"dump {d['date']} is not complete, jobs not done: {not_done}")


def download_file(url, target, session) -> None:
    """Stream `url` to `target`, resuming from a `.part` file if present."""
    part = target.with_suffix(target.suffix + ".part")
    pos = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={pos}-"} if pos else {}
    with session.get(url, headers=headers, stream=True, timeout=120) as r:
        if pos and r.status_code != 206:  # server ignored Range: restart from scratch
            pos = 0
        r.raise_for_status()
        total = pos + int(r.headers.get("Content-Length", 0))
        mode = "ab" if pos else "wb"
        done, last = pos, 0
        with open(part, mode) as f:
            for chunk in r.iter_content(CHUNK):
                f.write(chunk)
                done += len(chunk)
                if done - last > 200 * CHUNK:
                    log.info("%s: %.0f / %.0f MB", target.name, done / 2**20, total / 2**20)
                    last = done
    part.replace(target)


def main() -> None:
    cfg = load_config()
    d = cfg["dump"]
    raw = path(cfg, "raw")
    session = requests.Session()
    session.headers["User-Agent"] = cfg["user_agent"]
    check_dump_status(cfg, session)
    sums = fetch_checksums(cfg, session)

    manifest_path = raw / "manifest.json"
    manifest = read_json(manifest_path) if manifest_path.exists() else {}
    for table in d["tables"]:
        name = dump_filename(cfg, table)
        url = f"{d['base_url']}/{d['wiki']}/{d['date']}/{name}"
        target = raw / name
        expected = sums[name]
        if target.exists() and manifest.get(name, {}).get("sha1") == expected:
            log.info("%s already present and verified, skipping", name)
            continue
        if not target.exists():
            log.info("downloading %s", url)
            download_file(url, target, session)
        actual = sha1_of(target)
        if actual != expected:
            target.unlink()
            sys.exit(f"checksum mismatch for {name}: {actual} != {expected} (file deleted, re-run)")
        log.info("%s verified (sha1 %s)", name, actual)
        manifest[name] = {
            "url": url,
            "size_bytes": target.stat().st_size,
            "sha1": actual,
            "acquired_utc": manifest.get(name, {}).get("acquired_utc")
            or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        write_json(manifest, manifest_path)
    log.info("all files present and verified")


if __name__ == "__main__":
    main()
