"""Reader for the MAPS_SPEC data formats (stdlib only).

The selector (W2) owns the runtime loader; this is the calibration harness's
independent reader. It is used for three things:

* `bench/probe.py` ad-hoc queries,
* the reference rule engine (so variants can be A/B'd before `blastradius` lands),
* validating mock map sets, which are generated in exactly the spec formats.

Formats (from MAPS_SPEC.md):

* `tests_index.json`      `{"test_ids": [...]}`; ordinal = position
* `coverage.bin.gz`       `b"BRcov1"`, u32 nfiles, nfiles x (u16 len, name, u64 offset),
                          then per file: u32 ntests, ntests x (u32 ordinal, u32 n,
                          n x (u32 start, u32 run_len))  -- 1-based inclusive runs
* `symbols.json.gz`       `{path: [[qualname, start, end, kind], ...]}`
* `usage.json.gz`         `{path: {NAME: [line, ...]}}`
* `fixtures.json.gz`      `{fixture: [test id]}` plus `__test_files__`, `__shared__`
* `source.tar.gz`         pinned `sqlglot/**/*.py`, repo-relative member names
* `MANIFEST.json`         generator metadata (optional for the bench)
"""

from __future__ import annotations

import bisect
import gzip
import json
import struct
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

COVERAGE_MAGIC = b"BRcov1"


class MapError(Exception):
    pass


@dataclass(frozen=True)
class Symbol:
    qualname: str
    start: int
    end: int
    kind: str  # "function" | "class"

    def overlaps(self, start: int, end: int) -> bool:
        return self.start <= end and start <= self.end


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort and coalesce inclusive 1-based ranges."""
    cleaned = [(max(1, int(a)), int(b)) for a, b in ranges if b >= a]
    cleaned.sort()
    merged: list[tuple[int, int]] = []
    for start, end in cleaned:
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


class CoverageIndex:
    """Per-file interval index: (start, end, test ordinal)."""

    def __init__(self, intervals: dict[str, list[tuple[int, int, int]]]):
        self._intervals = {path: sorted(items) for path, items in intervals.items()}

    def files(self) -> list[str]:
        return list(self._intervals)

    def intervals(self, path: str) -> list[tuple[int, int, int]]:
        return self._intervals.get(path, [])

    def ordinals_covering(self, path: str, ranges: list[tuple[int, int]]) -> set[int]:
        intervals = self._intervals.get(path)
        merged = merge_ranges(ranges)
        if not intervals or not merged:
            return set()
        starts = [start for start, _ in merged]
        prefix_ends: list[int] = []
        best = 0
        for _, end in merged:
            best = max(best, end)
            prefix_ends.append(best)
        out: set[int] = set()
        for start, end, ordinal in intervals:
            index = bisect.bisect_right(starts, end) - 1
            if index >= 0 and prefix_ends[index] >= start:
                out.add(ordinal)
        return out

    def lines_covered_by(self, path: str, ordinal: int) -> list[tuple[int, int]]:
        return [
            (start, end)
            for start, end, item_ordinal in self._intervals.get(path, [])
            if item_ordinal == ordinal
        ]


class MapSet:
    """Lazily loaded map bundle; missing pieces degrade to empty + a warning."""

    def __init__(self, directory: Path, warn=None):
        self.directory = Path(directory)
        self.warn = warn or (lambda message: print(f"[bench] {message}", file=sys.stderr))
        self.tests_index: list[str] = []
        self.manifest: dict = {}
        self.coverage = CoverageIndex({})
        self.symbols: dict[str, list[Symbol]] = {}
        self.usage: dict[str, dict[str, list[int]]] = {}
        self.fixtures: dict[str, list[str]] = {}
        self.test_files: dict[str, list[str]] = {}
        self.shared: list[str] = []
        self._sources: dict[str, str] | None = None
        self._ordinal_to_id: dict[int, str] = {}
        self._id_to_ordinal: dict[str, int] = {}
        self.loaded_parts: list[str] = []
        self.missing_parts: list[str] = []

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(cls, directory: str | Path, warn=None) -> "MapSet":
        bundle = cls(Path(directory), warn=warn)
        bundle._load()
        return bundle

    def _mark(self, name: str, present: bool, loader) -> None:
        if not present:
            self.missing_parts.append(name)
            return
        try:
            loader()
            self.loaded_parts.append(name)
        except Exception as error:  # noqa: BLE001 - a bad map must not kill the bench
            self.missing_parts.append(name)
            self.warn(f"failed to load {name} from {self.directory}: {error}")

    def _load(self) -> None:
        directory = self.directory
        tests_index = directory / "tests_index.json"
        coverage = directory / "coverage.bin.gz"
        symbols = directory / "symbols.json.gz"
        usage = directory / "usage.json.gz"
        fixtures = directory / "fixtures.json.gz"
        manifest = directory / "MANIFEST.json"

        def read_tests() -> None:
            payload = json.loads(tests_index.read_text("utf-8"))
            ids = payload.get("test_ids", [])
            self.tests_index = [str(item) for item in ids]
            self._ordinal_to_id = dict(enumerate(self.tests_index))
            self._id_to_ordinal = {test_id: ordinal for ordinal, test_id in self._ordinal_to_id.items()}

        def read_coverage() -> None:
            payload = gzip.decompress(coverage.read_bytes())
            self.coverage = CoverageIndex(_parse_coverage(payload))

        def read_symbols() -> None:
            payload = _read_json_gz(symbols)
            self.symbols = {
                str(path): [
                    Symbol(str(entry[0]), int(entry[1]), int(entry[2]), str(entry[3]))
                    for entry in entries
                    if len(entry) >= 4
                ]
                for path, entries in payload.items()
            }

        def read_usage() -> None:
            payload = _read_json_gz(usage)
            self.usage = {
                str(path): {str(name): [int(line) for line in lines] for name, lines in names.items()}
                for path, names in payload.items()
            }

        def read_fixtures() -> None:
            payload = _read_json_gz(fixtures)
            self.shared = [str(item) for item in payload.pop("__shared__", [])]
            self.test_files = {
                str(path): [str(item) for item in tests]
                for path, tests in payload.pop("__test_files__", {}).items()
            }
            self.fixtures = {
                str(path): [str(item) for item in tests]
                for path, tests in payload.items()
            }

        def read_manifest() -> None:
            self.manifest = json.loads(manifest.read_text("utf-8"))

        self._mark("tests_index", tests_index.is_file(), read_tests)
        self._mark("coverage", coverage.is_file(), read_coverage)
        self._mark("symbols", symbols.is_file(), read_symbols)
        self._mark("usage", usage.is_file(), read_usage)
        self._mark("fixtures", fixtures.is_file(), read_fixtures)
        self._mark("manifest", manifest.is_file(), read_manifest)

    # ---------------------------------------------------------------- sources
    def _load_sources(self) -> dict[str, str]:
        if self._sources is not None:
            return self._sources
        archive = self.directory / "source.tar.gz"
        sources: dict[str, str] = {}
        if not archive.is_file():
            self.missing_parts.append("source")
            self._sources = sources
            return sources
        try:
            with tarfile.open(archive, "r:gz") as tar:
                for member in tar.getmembers():
                    if not member.isfile():
                        continue
                    name = member.name
                    if name.startswith("./"):
                        name = name[2:]
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        continue
                    sources[name] = extracted.read().decode("utf-8", "replace")
            self.loaded_parts.append("source")
        except Exception as error:  # noqa: BLE001
            self.missing_parts.append("source")
            self.warn(f"failed to load source.tar.gz: {error}")
        self._sources = sources
        return sources

    def source_text(self, path: str) -> str | None:
        return self._load_sources().get(path)

    def file_line_count(self, path: str) -> int | None:
        text = self.source_text(path)
        if text is not None:
            return len(text.splitlines())
        return None

    # ---------------------------------------------------------------- queries
    def test_id(self, ordinal: int) -> str | None:
        return self._ordinal_to_id.get(ordinal)

    def ordinal_of(self, test_id: str) -> int | None:
        return self._id_to_ordinal.get(test_id)

    def all_tests(self) -> set[str]:
        return set(self.tests_index)

    def known_source_file(self, path: str) -> bool:
        if path in self.symbols or path in self.coverage.files():
            return True
        return self.source_text(path) is not None

    def symbol_list(self, path: str) -> list[Symbol]:
        return self.symbols.get(path, [])

    def tests_covering_ranges(self, path: str, ranges: list[tuple[int, int]]) -> set[str]:
        ordinals = self.coverage.ordinals_covering(path, ranges)
        ids = (self.test_id(ordinal) for ordinal in ordinals)
        return {test for test in ids if test is not None}

    def usage_names(self, path: str) -> dict[str, list[int]]:
        return self.usage.get(path, {})

    def fixture_tests(self, path: str) -> list[str] | None:
        tests = self.fixtures.get(path)
        return None if tests is None else list(tests)

    def is_shared(self, path: str) -> bool:
        return path in self.shared

    def test_file_tests(self, path: str) -> list[str] | None:
        tests = self.test_files.get(path)
        return None if tests is None else list(tests)

    def is_mock(self) -> bool:
        return str(self.manifest.get("source", "")) == "mock"

    def describe(self) -> dict:
        return {
            "directory": str(self.directory),
            "loaded": list(self.loaded_parts),
            "missing": list(self.missing_parts),
            "tests": len(self.tests_index),
            "files": len(self.symbols),
            "mock": self.is_mock(),
        }


def _read_json_gz(path: Path) -> dict:
    return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))


def _parse_coverage(buffer: bytes) -> dict[str, list[tuple[int, int, int]]]:
    if len(buffer) < 8 or buffer[:6] != COVERAGE_MAGIC:
        raise MapError("coverage.bin.gz: missing BRcov1 magic")
    (nfiles,) = struct.unpack_from(">I", buffer, 6)
    offset = 10
    file_offsets: list[tuple[str, int]] = []
    for _ in range(nfiles):
        (name_len,) = struct.unpack_from(">H", buffer, offset)
        offset += 2
        name = buffer[offset : offset + name_len].decode("utf-8")
        offset += name_len
        (block_offset,) = struct.unpack_from(">Q", buffer, offset)
        offset += 8
        file_offsets.append((name, block_offset))

    intervals: dict[str, list[tuple[int, int, int]]] = {}
    for name, block_offset in file_offsets:
        cursor = block_offset
        (ntests,) = struct.unpack_from(">I", buffer, cursor)
        cursor += 4
        items: list[tuple[int, int, int]] = []
        for _ in range(ntests):
            ordinal, nintervals = struct.unpack_from(">II", buffer, cursor)
            cursor += 8
            for _ in range(nintervals):
                start, run_len = struct.unpack_from(">II", buffer, cursor)
                cursor += 8
                end = start + run_len - 1
                items.append((start, end, ordinal))
        intervals[name] = items
    return intervals
