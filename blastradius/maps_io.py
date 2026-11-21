"""Readers for the committed dependency maps (formats in MAPS_SPEC.md).

Everything here is lazy where it matters: the coverage header is parsed once
(gzip inflate), but the per-file test/interval payload is only turned into a
query index the first time that file is selected.  Symbols / usage / fixtures
JSON are loaded on first use.  The source tarball is only opened when a
comment-neutrality check or a file-length check needs it.

Coverage queries are answered from a block index: every ``BLOCK_LINES``-line
block maps to a bitmask of test ordinals whose recorded runs touch it.  That
over-approximates by at most ``BLOCK_LINES - 1`` lines (recall-safe) and keeps
queries to a handful of integer ORs.
"""

from __future__ import annotations

import ast
import gzip
import json
import struct
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


class MapsError(RuntimeError):
    """The committed maps are missing or unreadable."""


# Max distance (in lines) between a queried line and a selected test's coverage.
BLOCK_LINES = 4

_COVERAGE_MAGIC = b"BRcov1"
_U16 = struct.Struct(">H")
_U32 = struct.Struct(">I")
_U64 = struct.Struct(">Q")
_U32X2 = struct.Struct(">II")

_MAX_ORDINAL = 8192
_BITS = [1 << ordinal for ordinal in range(4096)]


@dataclass(frozen=True)
class Symbol:
    qualname: str
    start: int
    end: int
    kind: str

    @property
    def span(self) -> tuple[int, int]:
        return self.start, self.end


@dataclass(frozen=True)
class Reference:
    file: str
    line: int


def _read_json_gz(path: Path) -> dict:
    with gzip.open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def _query_ranges(queries: Iterable[int | tuple[int, int]]):
    for query in queries:
        if isinstance(query, int):
            yield query, query
        else:
            start, end = query
            yield int(start), int(end)


class _Coverage:
    """Index over ``coverage.bin.gz`` (header eagerly, payload lazily)."""

    def __init__(self, data: bytes) -> None:
        if data[: len(_COVERAGE_MAGIC)] != _COVERAGE_MAGIC:
            raise MapsError("coverage.bin.gz has a bad magic header")
        (file_count,) = _U32.unpack_from(data, len(_COVERAGE_MAGIC))
        position = len(_COVERAGE_MAGIC) + 4
        offsets: dict[str, int] = {}
        for _ in range(file_count):
            (name_length,) = _U16.unpack_from(data, position)
            position += 2
            name = data[position : position + name_length].decode("utf-8")
            position += name_length
            (offset,) = _U64.unpack_from(data, position)
            position += 8
            offsets[name] = offset
        self.data = data
        self.offsets = offsets
        self._indexes: dict[str, tuple[dict[int, int], int] | None] = {}

    def __contains__(self, file: str) -> bool:
        return file in self.offsets

    def _index(self, file: str) -> tuple[dict[int, int], int] | None:
        if file in self._indexes:
            return self._indexes[file]
        offset = self.offsets.get(file)
        if offset is None:
            self._indexes[file] = None
            return None

        data = memoryview(self.data)
        (test_count,) = _U32.unpack_from(data, offset)
        position = offset + _U32.size
        masks: dict[int, int] = {}
        max_line = 0
        for _ in range(test_count):
            ordinal, interval_count = _U32X2.unpack_from(data, position)
            position += _U32X2.size
            end = position + interval_count * _U32X2.size
            if ordinal < _MAX_ORDINAL:
                bit = _BITS[ordinal] if ordinal < len(_BITS) else 1 << ordinal
                for start, run_length in _U32X2.iter_unpack(data[position:end]):
                    if run_length == 0:
                        continue
                    last = start + run_length - 1
                    if last > max_line:
                        max_line = last
                    first_block = (start - 1) // BLOCK_LINES
                    last_block = (last - 1) // BLOCK_LINES
                    for block in range(first_block, last_block + 1):
                        masks[block] = masks.get(block, 0) | bit
            position = end
        index = (masks, max_line)
        self._indexes[file] = index
        return index

    def covering(self, file: str, queries: Iterable[int | tuple[int, int]]) -> set[int]:
        index = self._index(file)
        if index is None:
            return set()
        masks, max_line = index
        if not masks:
            return set()
        mask = 0
        for start, end in _query_ranges(queries):
            if start < 1:
                start = 1
            if max_line and end > max_line:
                end = max_line
            if start > end:
                continue
            first_block = (start - 1) // BLOCK_LINES
            last_block = (end - 1) // BLOCK_LINES
            for block in range(first_block, last_block + 1):
                block_mask = masks.get(block)
                if block_mask:
                    mask |= block_mask
        result: set[int] = set()
        while mask:
            low = mask & -mask
            result.add(low.bit_length() - 1)
            mask ^= low
        return result


def _collect_assignments(tree: ast.AST) -> tuple[tuple[int, int, tuple[str, ...]], ...]:
    """Module/class-level assignments as ``(start, end, names)`` spans.

    Function bodies are skipped (their symbols are handled by the symbol map).
    Target names are collected with a tiny recursive walk; ``ast.walk`` over
    every target was a measurable cost on assignment-heavy modules.
    """
    spans: list[tuple[int, int, tuple[str, ...]]] = []

    def target_names(targets: Sequence[ast.AST]) -> set[str]:
        names: set[str] = set()

        def visit(node: ast.AST) -> None:
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, (ast.Tuple, ast.List)):
                for element in node.elts:
                    visit(element)
            elif isinstance(node, ast.Starred):
                visit(node.value)

        for target in targets:
            visit(target)
        return names

    def add(node: ast.AST, names: Iterable[str]) -> None:
        unique = tuple(sorted(set(names)))
        if not unique:
            return
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", start)
        if start is None:
            return
        spans.append((int(start), int(end if end is not None else start), unique))

    def visit_body(body: Sequence[ast.stmt]) -> None:
        for statement in body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(statement, ast.ClassDef):
                visit_body(statement.body)
                continue
            if isinstance(statement, ast.Assign):
                add(statement, target_names(statement.targets))
                continue
            if isinstance(statement, ast.AnnAssign):
                if statement.value is not None:
                    add(statement, target_names([statement.target]))
                continue
            if isinstance(statement, ast.AugAssign):
                add(statement, target_names([statement.target]))
                continue
            if isinstance(statement, ast.Import):
                add(
                    statement,
                    {alias.asname or alias.name.split(".")[0] for alias in statement.names},
                )
            elif isinstance(statement, ast.ImportFrom):
                add(
                    statement,
                    {
                        alias.asname or alias.name
                        for alias in statement.names
                        if alias.name != "*"
                    },
                )
            # Compound statements at module/class level keep that level.
            nested = getattr(statement, "body", None)
            if isinstance(nested, list):
                visit_body(nested)
            for handler in getattr(statement, "handlers", ()) or ():
                visit_body(handler.body)
            for field in ("orelse", "finalbody"):
                child = getattr(statement, field, None)
                if isinstance(child, list):
                    visit_body(child)

    visit_body(getattr(tree, "body", []))
    spans.sort()
    return tuple(spans)


class Maps:
    """All committed maps, loaded from one directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.warnings: list[str] = []

        index_path = self.root / "tests_index.json"
        try:
            with index_path.open("r", encoding="utf-8") as handle:
                document = json.load(handle)
            ids = document["test_ids"]
            if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
                raise MapsError("tests_index.json: test_ids must be a list of strings")
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise MapsError(f"cannot read {index_path}: {error}") from error
        self._test_ids: list[str] = ids

        coverage_path = self.root / "coverage.bin.gz"
        try:
            coverage_bytes = gzip.decompress(coverage_path.read_bytes())
        except (OSError, EOFError, ValueError) as error:
            raise MapsError(f"cannot read {coverage_path}: {error}") from error
        self._coverage = _Coverage(coverage_bytes)

        self._symbols_raw: dict | None = None
        self._symbols_cache: dict[str, tuple[Symbol, ...] | None] = {}
        self._usage_raw: dict | None = None
        self._usage_refs_raw: dict | None = None
        self._fixtures_raw: dict | None = None
        self._shared: frozenset[str] | None = None
        self._tar: tarfile.TarFile | None = None
        self._tar_unavailable = False
        self._source_cache: dict[str, bytes | None] = {}
        self._text_cache: dict[str, str | None] = {}
        self._assignments_cache: dict[str, tuple[tuple[int, int, tuple[str, ...]], ...]] = {}

    # -- lifecycle -------------------------------------------------------

    @classmethod
    def load(cls, root: str | Path) -> "Maps":
        return cls(Path(root))

    def close(self) -> None:
        if self._tar is not None:
            try:
                self._tar.close()
            finally:
                self._tar = None

    # -- tests -----------------------------------------------------------

    def test_ids(self) -> list[str]:
        return self._test_ids

    # -- optional JSON maps ---------------------------------------------

    def _optional_json(self, filename: str) -> dict:
        path = self.root / filename
        try:
            return _read_json_gz(path)
        except (OSError, ValueError) as error:
            if filename != "usage_refs.json.gz":  # optional cross-file extension
                self.warnings.append(f"{filename}: {error}")
            return {}

    def _symbols(self) -> dict:
        if self._symbols_raw is None:
            self._symbols_raw = self._optional_json("symbols.json.gz")
        return self._symbols_raw

    def _usage(self) -> dict:
        if self._usage_raw is None:
            self._usage_raw = self._optional_json("usage.json.gz")
        return self._usage_raw

    def _usage_refs(self) -> dict:
        """Cross-file references (``usage_refs.json.gz``, W1 extension)."""
        if self._usage_refs_raw is None:
            self._usage_refs_raw = self._optional_json("usage_refs.json.gz")
        return self._usage_refs_raw

    def _fixtures(self) -> dict:
        if self._fixtures_raw is None:
            self._fixtures_raw = self._optional_json("fixtures.json.gz")
        return self._fixtures_raw

    # -- symbols ---------------------------------------------------------

    def symbols_of(self, file: str) -> tuple[Symbol, ...] | None:
        """Symbols for a source file, or ``None`` if the file is unknown."""
        if file in self._symbols_cache:
            return self._symbols_cache[file]
        raw = self._symbols().get(file)
        if raw is None:
            symbols: tuple[Symbol, ...] | None = None
        else:
            parsed: list[Symbol] = []
            for entry in raw:
                try:
                    qualname, start, end, kind = entry
                except (TypeError, ValueError):
                    continue
                parsed.append(Symbol(str(qualname), int(start), int(end), str(kind)))
            symbols = tuple(parsed)
        self._symbols_cache[file] = symbols
        return symbols

    # -- coverage --------------------------------------------------------

    def covers_file(self, file: str) -> bool:
        return file in self._coverage

    def covering_tests(
        self, file: str, queries: Iterable[int | tuple[int, int]]
    ) -> set[int]:
        """Test ordinals covering any queried line/range of ``file``."""
        return self._coverage.covering(file, queries)

    # -- usage -----------------------------------------------------------

    def usage_lines(self, file: str, name: str) -> list[int] | None:
        table = self._usage().get(file)
        if not isinstance(table, dict):
            return None
        lines = table.get(name)
        if lines is None:
            return None
        return [int(line) for line in lines]

    def references_of(self, name: str) -> list[Reference]:
        """Every (file, line) referencing ``name``, unioned across both maps.

        ``usage.json.gz`` keys by the *defining* file (W1 layout), while
        ``usage_refs.json.gz`` is ``{NAME: {file: [lines]}}`` for names whose
        references reach another file.  Taking the union is correct for both
        layouts and keeps cross-module recall.
        """
        seen: set[tuple[str, int]] = set()
        references: list[Reference] = []

        cross = self._usage_refs().get(name)
        if isinstance(cross, dict):
            for file, lines in cross.items():
                if not isinstance(lines, list):
                    continue
                for line in lines:
                    key = (str(file), int(line))
                    if key not in seen:
                        seen.add(key)
                        references.append(Reference(*key))

        for file, table in self._usage().items():
            if not isinstance(table, dict):
                continue
            lines = table.get(name)
            if not isinstance(lines, list):
                continue
            for line in lines:
                key = (file, int(line))
                if key not in seen:
                    seen.add(key)
                    references.append(Reference(*key))

        references.sort(key=lambda reference: (reference.file, reference.line))
        return references

    # -- fixtures / test files ------------------------------------------

    def fixture_readers(self, path: str) -> tuple[str, ...] | None:
        fixtures = self._fixtures()
        readers = fixtures.get(path)
        if readers is None:
            return None
        if not isinstance(readers, list):
            return None
        return tuple(str(item) for item in readers)

    def test_file_tests(self, path: str) -> tuple[str, ...] | None:
        fixtures = self._fixtures()
        table = fixtures.get("__test_files__")
        if not isinstance(table, dict):
            return None
        tests = table.get(path)
        if not isinstance(tests, list):
            return None
        return tuple(str(item) for item in tests)

    def shared_files(self) -> frozenset[str]:
        if self._shared is None:
            fixtures = self._fixtures()
            shared = fixtures.get("__shared__")
            if isinstance(shared, list):
                self._shared = frozenset(str(item) for item in shared)
            else:
                self._shared = frozenset()
        return self._shared

    # -- pinned source ---------------------------------------------------

    def _open_tar(self) -> tarfile.TarFile | None:
        if self._tar is not None:
            return self._tar
        if self._tar_unavailable:
            return None
        path = self.root / "source.tar.gz"
        try:
            self._tar = tarfile.open(path, mode="r:gz")
        except (OSError, tarfile.TarError) as error:
            self.warnings.append(f"source.tar.gz: {error}")
            self._tar_unavailable = True
            return None
        return self._tar

    def source_bytes(self, file: str) -> bytes | None:
        if file in self._source_cache:
            return self._source_cache[file]
        archive = self._open_tar()
        if archive is None:
            return None
        data: bytes | None = None
        for name in (file, "./" + file):
            try:
                member = archive.getmember(name)
            except KeyError:
                continue
            if not member.isfile():
                continue
            handle = archive.extractfile(member)
            if handle is not None:
                data = handle.read()
            break
        self._source_cache[file] = data
        return data

    def source_text(self, file: str) -> str | None:
        if file in self._text_cache:
            return self._text_cache[file]
        data = self.source_bytes(file)
        text: str | None = None
        if data is not None:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = None
        self._text_cache[file] = text
        return text

    def file_length(self, file: str) -> int | None:
        text = self.source_text(file)
        if text is None:
            return None
        return len(text.splitlines())

    def assignments_on_lines(self, file: str, lines: Iterable[int]) -> tuple[str, ...]:
        """Names assigned at module/class level on any of ``lines`` (pinned AST)."""
        text = self.source_text(file)
        if text is None:
            return ()
        spans = self._assignments_cache.get(file)
        if spans is None:
            try:
                tree = ast.parse(text)
            except SyntaxError:
                spans = ()
            else:
                spans = _collect_assignments(tree)
            self._assignments_cache[file] = spans
        wanted = set(lines)
        if not wanted:
            return ()
        names: set[str] = set()
        for start, end, assigned in spans:
            if any(start <= line <= end for line in wanted):
                names.update(assigned)
        return tuple(sorted(names))
