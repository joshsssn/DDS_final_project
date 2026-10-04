"""Unit tests of the SQL dump parser on tricky rows."""
import gzip

import pyarrow.parquet as pq
import pytest

from wikigame import parse_sql as ps

STR3 = [(0, "int32"), (1, "int16"), (2, "str")]


def parse(body: bytes, ncols=3, wanted=STR3, row_filter=None):
    n, cols = ps.parse_body(body, ncols, wanted, row_filter)
    return n, [list(cols[i]) for i, _ in wanted]


def test_simple_rows():
    n, (ids, ns, titles) = parse(b"(1,0,'Paris'),(2,14,'Cat')")
    assert n == 2 and ids == [1, 2] and ns == [0, 14] and titles == ["Paris", "Cat"]


def test_escaped_quote_and_backslash():
    n, (_, _, t) = parse(rb"(1,0,'L\'Isle-Adam'),(2,0,'a\\b'),(3,0,'\\\'')")
    assert t == ["L'Isle-Adam", "a\\b", "\\'"]


def test_separators_inside_strings():
    # '),(' and ',' inside a quoted string must not split rows
    n, (ids, _, t) = parse(b"(1,0,'a),(b'),(2,0,'x,y')")
    assert n == 2 and ids == [1, 2] and t == ["a),(b", "x,y"]


def test_utf8_titles():
    title = "Île-de-France_—_Œuvre_日本"
    body = b"(7,0,'" + title.encode("utf-8") + b"')"
    assert parse(body)[1][2] == [title]


def test_null_and_numbers():
    wanted = [(0, "int32"), (1, "str"), (2, "str")]
    n, (ids, a, b) = parse(b"(5,NULL,''),(6,'x',NULL)", wanted=wanted)
    assert ids == [5, 6] and a == [None, "x"] and b == ["", None]


def test_mysql_escapes():
    n, (_, _, t) = parse(rb"(1,0,'a\nb\tc\0d\"e')")
    assert t == ['a\nb\tc\x00d"e']


def test_float_and_negative():
    wanted = [(0, "int32"), (2, "int64")]
    n, (a, c) = parse(b"(1,0.123,-5),(2,1e-05,7)", wanted=wanted)
    assert a == [1, 2] and c == [-5, 7]


def test_fast_integer_path():
    wanted = [(0, "int32"), (1, "int16"), (2, "int64")]
    n, (a, b, c) = parse(b"(1,0,10),(2,0,20),(3,4,30)", wanted=wanted)
    assert n == 3 and a == [1, 2, 3] and b == [0, 0, 4] and c == [10, 20, 30]


def test_wrong_column_count_raises():
    with pytest.raises(ps.ParseError):
        parse(b"(1,0,'a'),(2,0)")
    with pytest.raises(ps.ParseError):
        parse(b"(1,0,10),(2,0)", wanted=[(0, "int32"), (1, "int16"), (2, "int64")])


def test_garbage_between_rows_raises():
    with pytest.raises(ps.ParseError):
        parse(b"(1,0,'a') (2,0,'b')")


def test_row_filter():
    wanted = [(0, "int32"), (1, "str")]
    n, cols = ps.parse_body(
        b"(1,'disambiguation',''),(2,'wikibase_item','Q1'),(3,'disambiguation','')",
        3, wanted, (1, b"'disambiguation'"))
    assert n == 3 and cols[0] == [1, 3]


def test_read_create_table():
    lines = [b"-- header\n", b"CREATE TABLE `page` (\n", b"  `page_id` int(8) unsigned NOT NULL,\n",
             b"  `page_namespace` int(11) NOT NULL,\n", b"  PRIMARY KEY (`page_id`),\n",
             b") ENGINE=InnoDB;\n"]
    assert ps.read_create_table(iter(lines), "page") == ["page_id", "page_namespace"]


def test_parse_table_end_to_end(tmp_path):
    """Full file round trip through gzip, CREATE TABLE and the worker pool."""
    raw = tmp_path / "raw"
    raw.mkdir()
    content = (
        b"CREATE TABLE `linktarget` (\n  `lt_id` bigint(20) unsigned NOT NULL,\n"
        b"  `lt_namespace` int(11) NOT NULL,\n  `lt_title` varbinary(255) NOT NULL,\n"
        b"  PRIMARY KEY (`lt_id`)\n) ENGINE=InnoDB;\n"
        b"INSERT INTO `linktarget` VALUES (1,0,'Paris'),(2,0,'L\\'Ha'),(3,10,'X');\n"
        b"INSERT INTO `linktarget` VALUES (4,0,'\xc3\x89t\xc3\xa9');\n"
    )
    with gzip.open(raw / "frwiki-20000101-linktarget.sql.gz", "wb") as f:
        f.write(content)
    cfg = {"dump": {"wiki": "frwiki", "date": "20000101"},
           "paths": {"raw": str(raw), "interim": str(tmp_path / "interim")},
           "parse": {"workers": 2}}
    s = ps.parse_table(cfg, "linktarget")
    t = pq.read_table(tmp_path / "interim" / "linktarget.parquet").to_pydict()
    assert s["rows_in_dump"] == 4 and t["lt_id"] == [1, 2, 3, 4]
    assert t["lt_title"][1] == "L'Ha" and t["lt_title"][3] == "Été" and t["lt_namespace"][2] == 10


def test_insert_statements_both_layouts():
    p = b"INSERT INTO `t` VALUES"
    old = [b"junk\n", p + b" (1,0,'a'),(2,0,'b');\n", p + b" (3,0,'c');\n"]
    new = [p + b"\n", b"(1,0,'a'),\n", b"(2,0,'b');\n", p + b"\n", b"(3,0,'c');\n"]
    expected = [b"(1,0,'a'),(2,0,'b')", b"(3,0,'c')"]
    assert list(ps.insert_statements(iter(old), p)) == expected
    assert list(ps.insert_statements(iter(new), p)) == expected


def test_truncated_statement_raises():
    with pytest.raises(ps.ParseError):
        list(ps.insert_statements(iter([b"INSERT INTO `t` VALUES\n", b"(1,0,'a'),\n"]),
                                  b"INSERT INTO `t` VALUES"))
