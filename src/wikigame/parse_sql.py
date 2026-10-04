"""Phase 1b - streaming parser for MediaWiki MySQL dump files (*.sql.gz).

A dump file contains a `CREATE TABLE` statement followed by many very long lines
    INSERT INTO `table` VALUES (v1,v2,...),(v1,v2,...),...;
Values are integers, floats, NULL, or single-quoted strings with MySQL backslash
escapes (\\' \\\\ \\n \\0 ...). Titles are UTF-8 bytes.

Design
------
* The file is read line by line from the gzip stream: never loaded in memory.
* Column names and order are read from the `CREATE TABLE` statement of the file
  itself (the schema has changed over time, e.g. pagelinks -> linktarget in 2024),
  and the columns we need are looked up by name. A missing column is a hard error.
* Each INSERT line is parsed with one regular expression matching a full row of
  exactly `ncols` values. The parser checks that consecutive rows are separated by
  exactly one comma and that the whole line is consumed: any unexpected syntax raises
  an error instead of silently producing shifted columns.
* Lines without any quote and whose wanted columns are all integers (pagelinks)
  use a fast numpy path; its result is validated by counting rows.
* Lines are parsed in parallel worker processes; output is one Parquet file per table
  with only the needed columns, written in batches.
"""
from __future__ import annotations

import gzip
import re
import sys
from multiprocessing import Pool

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .common import dump_filename, get_logger, load_config, path, read_json, timed, write_json

log = get_logger("parse_sql")

# Columns kept for each table (name -> kind). Everything else is discarded.
TABLES: dict[str, dict[str, str]] = {
    "page": {"page_id": "int32", "page_namespace": "int16",
             "page_title": "str", "page_is_redirect": "bool", "page_latest": "int64"},
    "pagelinks": {"pl_from": "int32", "pl_from_namespace": "int16", "pl_target_id": "int64"},
    "linktarget": {"lt_id": "int64", "lt_namespace": "int16", "lt_title": "str"},
    "redirect": {"rd_from": "int32", "rd_namespace": "int16", "rd_title": "str",
                 "rd_interwiki": "str", "rd_fragment": "str"},
    "page_props": {"pp_page": "int32", "pp_propname": "str"},
}
# Optional row filters (column, required value). page_props has one row per
# (page, property); we only need the disambiguation flag.
ROW_FILTERS = {"page_props": ("pp_propname", "disambiguation")}

PA_TYPES = {"int16": pa.int16(), "int32": pa.int32(), "int64": pa.int64(),
            "str": pa.string(), "bool": pa.bool_()}

VALUE = rb"('(?:[^'\\]|\\.)*'|NULL|[-+0-9.eE]+)"
_ESCAPES = {b"0": b"\x00", b"b": b"\x08", b"n": b"\n", b"r": b"\r", b"t": b"\t",
            b"Z": b"\x1a", b"\\": b"\\", b"'": b"'", b'"': b'"'}
_ESC_RE = re.compile(rb"\\(.)", re.S)
_COL_RE = re.compile(rb"^\s+`([^`]+)`\s")


class ParseError(ValueError):
    pass


def unescape(raw: bytes) -> bytes:
    """Undo MySQL string escapes. Unknown escapes keep the escaped character."""
    if b"\\" not in raw:
        return raw
    return _ESC_RE.sub(lambda m: _ESCAPES.get(m.group(1), m.group(1)), raw)


def row_regex(ncols: int) -> re.Pattern:
    return re.compile(rb"\(" + rb",".join([VALUE] * ncols) + rb"\)", re.S)


def convert(v: bytes, kind: str):
    """Convert one raw SQL token to a Python value of the requested kind."""
    if v == b"NULL":
        return None
    if v[:1] == b"'":
        v = unescape(v[1:-1])
        if kind == "str":
            return v.decode("utf-8", errors="replace")
    elif kind == "str":
        return v.decode("ascii")
    if kind == "bool":
        return bool(int(v))
    return int(v)


def split_rows(body: bytes, rx: re.Pattern):
    """Yield tuples of raw tokens for each row of `body` = '(..),(..),...'.

    Raises ParseError if anything between/after rows is not exactly one comma."""
    pos = 0
    n = len(body)
    for m in rx.finditer(body):
        if m.start() != pos:
            raise ParseError(f"unexpected text at offset {pos}: {body[pos:pos + 80]!r}")
        yield m.groups()
        pos = m.end()
        if pos < n:
            if body[pos:pos + 1] != b",":
                raise ParseError(f"expected ',' at offset {pos}: {body[pos:pos + 80]!r}")
            pos += 1
    if pos != n:
        raise ParseError(f"line not fully consumed: {body[pos:pos + 80]!r}")


def parse_body(body: bytes, ncols: int, wanted: list[tuple[int, str]],
               row_filter: tuple[int, bytes] | None = None):
    """Parse an INSERT body. Returns (n_rows_total, {col_index: list_or_array})."""
    all_int = all(k.startswith("int") for _, k in wanted)
    if all_int and row_filter is None and b"'" not in body and b"N" not in body:
        # Fast path: integers only. '(1,2,3),(4,5,6)' -> '1,2,3,4,5,6'.
        # Any surprise (float, bad token, wrong count) falls back to the strict path.
        flat = body[1:-1].replace(b"),(", b",").decode("ascii")
        nrows = body.count(b"),(") + 1
        try:
            arr = np.fromstring(flat, dtype=np.int64, sep=",")
        except ValueError:
            arr = None
        if arr is not None and arr.size == nrows * ncols:
            arr = arr.reshape(nrows, ncols)
            return nrows, {i: arr[:, i] for i, _ in wanted}

    rx = row_regex(ncols)
    cols: dict[int, list] = {i: [] for i, _ in wanted}
    nrows = 0
    for tok in split_rows(body, rx):
        nrows += 1
        if row_filter is not None:
            fi, fval = row_filter
            if tok[fi] != fval:
                continue
        for i, kind in wanted:
            cols[i].append(convert(tok[i], kind))
    return nrows, cols


def read_create_table(lines, table: str) -> list[str]:
    """Consume lines up to the end of CREATE TABLE and return column names in order."""
    start = f"CREATE TABLE `{table}` (".encode()
    cols: list[str] = []
    inside = False
    for line in lines:
        if line.startswith(start):
            inside = True
            continue
        if inside:
            if line.startswith(b")"):
                return cols
            m = _COL_RE.match(line)
            if m:
                cols.append(m.group(1).decode())
    raise ParseError(f"CREATE TABLE `{table}` not found")


# ---- multiprocessing worker ---------------------------------------------------
_W: dict = {}


def _init_worker(ncols, wanted, row_filter):
    _W.update(ncols=ncols, wanted=wanted, row_filter=row_filter)


def _work(body: bytes):
    return parse_body(body, _W["ncols"], _W["wanted"], _W["row_filter"])


def insert_statements(lines, prefix: bytes):
    """Yield the VALUES part of each INSERT statement, as one bytes string '(..),(..)'.

    Two layouts exist: historically each statement was one huge line
    ('INSERT INTO `t` VALUES (..),(..);'); since 2026 dumps put 'INSERT INTO `t` VALUES'
    alone on a line followed by one row per line. Raw newlines only occur between rows
    (newlines inside strings are escaped as \n), so stripping line ends and
    concatenating reconstructs the single-line form in both cases."""
    parts: list[bytes] | None = None
    for line in lines:
        if parts is None:
            if not line.startswith(prefix):
                continue
            parts = []
            line = line[len(prefix):].lstrip(b" ")
        chunk = line.rstrip(b"\r\n")
        if chunk.endswith(b";"):
            parts.append(chunk[:-1])
            yield b"".join(parts)
            parts = None
        elif chunk:
            parts.append(chunk)
    if parts is not None:
        raise ParseError("file ends inside an INSERT statement (truncated dump?)")


def parse_table(cfg: dict, table: str) -> dict:
    src = path(cfg, "raw") / dump_filename(cfg, table)
    out = path(cfg, "interim") / f"{table}.parquet"
    spec = TABLES[table]
    with gzip.open(src, "rb") as fh:
        columns = read_create_table(fh, table)
        missing = [c for c in spec if c not in columns]
        if missing:
            raise ParseError(f"{table}: columns {missing} not in dump schema {columns}")
        wanted = [(columns.index(c), k) for c, k in spec.items()]
        rf = ROW_FILTERS.get(table)
        row_filter = (columns.index(rf[0]), f"'{rf[1]}'".encode()) if rf else None
        schema = pa.schema([(c, PA_TYPES[k]) for c, k in spec.items()])
        prefix = f"INSERT INTO `{table}` VALUES".encode()

        n_total = n_kept = n_lines = 0
        buf: dict[int, list] = {i: [] for i, _ in wanted}
        buffered = 0
        writer = pq.ParquetWriter(out, schema, compression="zstd")

        def flush():
            nonlocal buffered
            arrays = []
            for (i, kind), name in zip(wanted, spec):
                parts = buf[i]
                if kind.startswith("int"):
                    vals = np.concatenate([np.asarray(x, dtype=np.int64) for x in parts]) \
                        if parts else np.array([], dtype=np.int64)
                    # refuse silent overflow when narrowing
                    info = np.iinfo(kind)
                    if vals.size and (vals.min() < info.min or vals.max() > info.max):
                        raise ParseError(f"{table}.{name} does not fit in {kind}")
                    arrays.append(pa.array(vals.astype(kind)))
                else:
                    arrays.append(pa.array([v for x in parts for v in x], type=PA_TYPES[kind]))
                buf[i] = []
            writer.write_table(pa.Table.from_arrays(arrays, schema=schema))
            buffered = 0

        with Pool(cfg["parse"]["workers"], _init_worker,
                  (len(columns), wanted, row_filter)) as pool:
            for nrows, cols in pool.imap(_work, insert_statements(fh, prefix), chunksize=2):
                n_lines += 1
                n_total += nrows
                kept = len(next(iter(cols.values())))
                n_kept += kept
                for i in cols:
                    buf[i].append(cols[i])
                buffered += kept
                if buffered >= 5_000_000:
                    flush()
                if n_lines % 500 == 0:
                    log.info("%s: %d statements, %d rows", table, n_lines, n_total)
        flush()
        writer.close()
    return {"file": src.name, "columns_in_dump": columns, "columns_kept": list(spec),
            "row_filter": f"{rf[0]} = '{rf[1]}'" if rf else None,
            "insert_statements": n_lines, "rows_in_dump": n_total, "rows_kept": n_kept,
            "parquet_bytes": out.stat().st_size}


def main(tables: list[str] | None = None) -> None:
    cfg = load_config()
    stats_path = path(cfg, "interim") / "parse_stats.json"
    stats = read_json(stats_path) if stats_path.exists() else {}
    for table in tables or cfg["dump"]["tables"]:
        timing: dict = {}
        with timed(log, f"parse {table}", timing):
            s = parse_table(cfg, table)
        s["seconds"] = timing[f"parse {table}"]
        stats[table] = s
        write_json(stats, stats_path)
        log.info("%s: %s rows in dump, %s kept", table, s["rows_in_dump"], s["rows_kept"])


if __name__ == "__main__":
    main(sys.argv[1:] or None)
