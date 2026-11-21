"""MAPS_SPEC format reader, and that the mock maps are written in that format."""

import gzip
import json
import struct

from bench.mapio import CoverageIndex, MapSet, merge_ranges

import bench.mockmaps.build as build


def test_mock_manifest_marks_mock(mock_maps_dir):
    manifest = json.loads((mock_maps_dir / "MANIFEST.json").read_text("utf-8"))
    assert manifest["source"] == "mock"
    assert manifest["pinned_sha"] == build.PINNED_SHA


def test_loads_every_part(maps):
    assert maps.missing_parts == []
    assert len(maps.tests_index) == len(build.MOCK_TESTS)
    assert maps.test_id(0) == build.MOCK_TESTS[0][0]
    assert maps.ordinal_of(build.MOCK_TESTS[4][0]) == 4
    assert maps.is_mock()


def test_coverage_magic_and_offsets(mock_maps_dir):
    payload = gzip.decompress((mock_maps_dir / "coverage.bin.gz").read_bytes())
    assert payload[:6] == b"BRcov1"
    (nfiles,) = struct.unpack_from(">I", payload, 6)
    assert nfiles == len(build.MOCK_COVERAGE)
    cursor = 10
    names = []
    for _ in range(nfiles):
        (name_len,) = struct.unpack_from(">H", payload, cursor)
        cursor += 2
        name = payload[cursor : cursor + name_len].decode("utf-8")
        cursor += name_len
        (offset,) = struct.unpack_from(">Q", payload, cursor)
        cursor += 8
        assert offset < len(payload)
        names.append(name)
    assert names == sorted(build.MOCK_COVERAGE)


def test_coverage_roundtrip(tmp_path):
    coverage = {
        "a.py": [(0, 5, 9), (1, 1, 2), (1, 3, 4), (2, 40, 41)],
        "b.py": [],
    }
    build.write_coverage(tmp_path / "coverage.bin.gz", coverage)
    index = MapSet.load(tmp_path)  # missing json maps are tolerated
    assert index.coverage.ordinals_covering("a.py", [(1, 10)]) == {0, 1}
    assert index.coverage.ordinals_covering("a.py", [(10, 12)]) == set()
    assert index.coverage.ordinals_covering("a.py", [(41, 41)]) == {2}
    assert index.coverage.lines_covered_by("a.py", 1) == [(1, 2), (3, 4)]


def test_merge_ranges():
    assert merge_ranges([(5, 7), (1, 3), (4, 4), (10, 9)]) == [(1, 7)]
    assert merge_ranges([]) == []


def test_symbols_derived_from_pinned_sources(maps):
    bigquery = maps.symbol_list("sqlglot/parsers/bigquery.py")
    names = {symbol.qualname for symbol in bigquery}
    assert "BigQueryParser" in names
    assert any(name.startswith("BigQueryParser.") for name in names)
    assert maps.file_line_count("sqlglot/helper.py") == 498
    assert maps.known_source_file("sqlglot/helper.py")
    assert not maps.known_source_file("sqlglot/planner.py")


def test_fixture_and_test_file_maps(maps):
    tests = maps.fixture_tests("tests/fixtures/optimizer/pushdown_predicates.sql")
    assert tests == build.MOCK_FIXTURE_MAP["tests/fixtures/optimizer/pushdown_predicates.sql"]
    assert maps.fixture_tests("tests/fixtures/identity.sql") is None
    assert maps.is_shared("tests/helpers.py")
    assert maps.test_file_tests("tests/test_parser.py") is not None


def test_missing_map_set_degrades(tmp_path):
    bundle = MapSet.load(tmp_path)
    assert bundle.missing_parts
    assert bundle.tests_index == []
    assert bundle.all_tests() == set()
    assert bundle.tests_covering_ranges("sqlglot/x.py", [(1, 2)]) == set()


def test_coverage_index_queries():
    index = CoverageIndex({"f.py": [(1, 3, 7), (10, 12, 7), (20, 20, 8)]})
    assert index.ordinals_covering("f.py", [(11, 11)]) == {7}
    assert index.ordinals_covering("f.py", [(13, 19)]) == set()
    assert index.ordinals_covering("nope.py", [(1, 2)]) == set()
