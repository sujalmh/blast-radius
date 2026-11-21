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

The v2 maps (``dialects.json.gz``, ``keyreads.json.gz``, ``testimports.json.gz``)
are strictly optional refinements: a missing or malformed file disables the
corresponding narrowing rule and degrades to the unrefined selection.  The
large one (``keyreads``) is only opened when a rule actually asks for it.

The L4 maps (``classes.json.gz``: per-test instantiated expression classes plus
the class hierarchy; ``usage_style.json.gz``: per-name reference lines split
into ``self_lines`` / ``class_lines`` / ``instance_lines`` by receiver kind) are
optional in exactly the same way: when either file is missing, unreadable, or
the construction trace covers less than 90% of the committed test index, L4 is
disabled with a warning and the L1 rule is used unchanged.

The 3-way split is what makes L4 *sound* rather than merely narrow.  A name's
reads are split by receiver: ``self_lines`` run on ``self``/``cls``, i.e. inside
a method of the owning class (already covered by that class's symbol spans),
``class_lines`` resolve statically to a class/module, and ``instance_lines``
read through a plain variable or parameter.  Instance reads can only be dropped
for a class family whose *instance universe* is captured by a trace:

* expression classes -- ``classes.json.gz`` records every ``Expr`` construction,
  so "some test built this class or a descendant" is the universe;
* dialect classes -- ``dialects.json.gz`` records which dialects each test
  resolves, so "this test ran under the class's dialect or a derived one" is
  the universe (a ``dialect.WEEK_OFFSET`` read implies a dialect was resolved,
  but not *which* one -- that is exactly what exposure answers).

For every other family (``Tokenizer``/``Parser``/``Generator`` class bodies,
plain helpers) instance creation is not traced, so ``self_lines`` and
``instance_lines`` stay in the union: dropping them there would silently lose
tests.
"""

from __future__ import annotations

import ast
import gzip
import json
import re
import struct
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


class MapsError(RuntimeError):
    """The committed maps are missing or unreadable."""


# Max distance (in lines) between a queried line and a selected test's coverage.
BLOCK_LINES = 4

# L4 (class-attribute narrowing) needs these maps and a construction trace that
# covers at least this share of the committed test index.
CLASS_MAP_FILES = ("classes.json.gz", "usage_style.json.gz")
CLASS_COVERAGE_SHARE = 9  # per-mille-free form: touched * 10 >= index * 9

# Class families whose instance universe a trace captures, so L4 may ignore
# ``instance_lines`` for them.
#   * expression classes: root names of the expression hierarchy (the
#     construction trace in classes.json.gz wraps Expr.__init__);
#   * dialect classes: the root dialect base class (dialects.json.gz exposure).
EXPRESSION_ROOTS = frozenset({"Expr", "Expression"})
DIALECT_ROOT = "Dialect"

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


@dataclass(frozen=True)
class LiteralEntry:
    """A dict/list/tuple/set entry on a pinned source line (L3 input)."""

    name: str
    kind: str  # "dict" | "list" | "tuple" | "set"
    key: str | None  # keyreads KEY form; None when it cannot be rendered
    index: int | None
    ambiguous: bool = False


def _read_json_gz(path: Path) -> dict:
    with gzip.open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


_BASE_CLASS = re.compile(r"^[ \t]*class\s+\w+\s*\(([^)]*)\)", re.MULTILINE)


def _base_classes(text: str) -> Iterable[str]:
    """Base class names of every ``class X(...)`` in ``text`` (first component)."""
    for match in _BASE_CLASS.finditer(text):
        for part in match.group(1).split(","):
            name = part.strip().split(".")[0].strip()
            if name and name.isidentifier():
                yield name


def _key_text(node: ast.AST) -> str | None:
    """Keyreads KEY form for a literal key node (``str`` for strings, repr otherwise).

    ``None`` when the node is not a plain constant: the selector never guesses
    at enum members / computed keys (the L3 gate then keeps L1).
    """
    if isinstance(node, ast.Constant):
        value = node.value
        return value if isinstance(value, str) else repr(value)
    return None


def _query_ranges(queries: Iterable[int | tuple[int, int]]):
    for query in queries:
        if isinstance(query, int):
            yield query, query
        else:
            start, end = query
            yield int(start), int(end)


def _bare_name(node: str) -> str:
    """Last dotted component of a class node (``a.b.C`` -> ``C``)."""
    return node.rpartition(".")[2]


def _style_lines(detail: dict, bucket: str, legacy: str | None = None) -> list[int]:
    """Sorted-ish reference lines of one ``usage_style`` bucket.

    ``legacy`` is the pre-W1f bucket name to fall back to when ``bucket`` is
    absent (``other_lines`` == ``class_lines | instance_lines``).
    """
    lines = detail.get(bucket)
    if not isinstance(lines, list) and legacy is not None:
        lines = detail.get(legacy)
    if not isinstance(lines, list):
        return []
    result: list[int] = []
    for line in lines:
        try:
            result.append(int(line))
        except (TypeError, ValueError):
            continue
    return result


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


def _collect_import_spans(tree: ast.AST) -> tuple[tuple[int, int], ...]:
    """Spans of module-level ``import`` / ``from ... import`` statements."""
    spans: list[tuple[int, int]] = []
    for statement in getattr(tree, "body", []):
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            start = int(statement.lineno)
            end = int(getattr(statement, "end_lineno", start) or start)
            spans.append((start, end))
    return tuple(spans)


def _collect_literal_entries(
    tree: ast.AST,
) -> tuple[tuple[int, int, str, str, str | None, int | None, bool], ...]:
    """Literal entries (``start, end, name, kind, key, index, ambiguous``).

    Only module/class-body ``dict``/``list``/``tuple``/``set`` literals are
    recorded: those are the constants the key-read map proxies.  ``key`` is the
    keyreads KEY form for dict entries with constant / enum-attribute keys and
    ``None`` otherwise; ``index`` is the element position for sequences.
    """

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

    entries: list[tuple[int, int, str, str, str | None, int | None, bool]] = []

    def start_line(node: ast.AST) -> int | None:
        line = getattr(node, "lineno", None)
        return None if line is None else int(line)

    def end_line(node: ast.AST, fallback: int) -> int:
        line = getattr(node, "end_lineno", None)
        return fallback if line is None else int(line)

    def record(name: str, value: ast.AST) -> None:
        if not name:
            return
        if isinstance(value, ast.Dict):
            keys = [
                (key, item)
                for key, item in zip(value.keys, value.values)
                if key is not None
            ]
            rendered = [_key_text(key) for key, _ in keys]
            duplicates = {
                text
                for text in rendered
                if text is not None and rendered.count(text) > 1
            }
            for (key, item), text in zip(keys, rendered):
                first = start_line(key)
                if first is None:
                    first = start_line(item)
                if first is None:
                    continue
                last = end_line(item, first)
                ambiguous = text is None or text in duplicates
                entries.append((first, last, name, "dict", text, None, ambiguous))
            return
        kind = {
            ast.List: "list",
            ast.Tuple: "tuple",
            ast.Set: "set",
        }.get(type(value))
        if kind is None:
            return
        for index, element in enumerate(value.elts):
            first = start_line(element)
            if first is None:
                continue
            last = end_line(element, first)
            if kind == "set":
                # A set is membership-only: the keyreads KEY is the *value*
                # tested by ``in``, i.e. the element itself.
                text = _key_text(element)
                entries.append((first, last, name, kind, text, None, text is None))
                continue
            entries.append((first, last, name, kind, str(index), index, False))

    def visit_body(body: Sequence[ast.stmt]) -> None:
        for statement in body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(statement, ast.ClassDef):
                # Class bodies are where the key-read proxies live; nested
                # functions inside them are still excluded above.
                visit_body(statement.body)
                continue
            names: set[str] = set()
            value: ast.AST | None = None
            if isinstance(statement, ast.Assign):
                names = target_names(statement.targets)
                value = statement.value
            elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
                names = target_names([statement.target])
                value = statement.value
            if value is not None:
                for name in sorted(names):
                    record(name, value)
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
    return tuple(entries)


def _class_module_names(file: str) -> tuple[str, ...]:
    """Module spellings a class defined in ``file`` can be qualified with.

    ``sqlglot/expressions/ddl.py`` yields ``sqlglot.expressions.ddl`` and the
    package form ``sqlglot.expressions`` (the spelling a construction tracer
    records for classes reached through the package's re-exports).
    """
    without = file[: -len(".py")] if file.endswith(".py") else file
    parts = [part for part in without.split("/") if part]
    if parts and parts[-1] == "__init__":
        parts.pop()
    if not parts:
        return ()
    names = [".".join(parts)]
    if len(parts) > 1:
        names.append(".".join(parts[:-1]))
    return tuple(dict.fromkeys(names))


def _class_spellings(file: str, qualname: str) -> tuple[str, ...]:
    """Every module-qualified spelling of ``qualname`` defined in ``file``."""
    return tuple(
        f"{module}.{qualname}" for module in _class_module_names(file) if module
    )


class _ClassGraph:
    """Construction + inheritance graph over ``classes.json.gz``.

    Nodes are canonical spellings: a class is addressable both as
    ``sqlglot.expressions.ddl.TriggerProperties`` (file-derived) and as
    ``sqlglot.expressions.TriggerProperties`` (package re-export form), and the
    symbol table folds both spellings onto one node, so the tracer's spelling
    and the selector's spelling always meet.
    """

    def __init__(
        self,
        hierarchy: dict,
        touched: dict,
        spans: dict[str, tuple[str, int, int]],
        alias: dict[str, str],
        unique_paths: dict[str, str],
    ) -> None:
        self.spans = spans
        self._alias = alias
        self._unique_paths = unique_paths
        self._edges: dict[str, set[str]] = {}
        self.touched: dict[str, frozenset[str]] = {}
        self._ancestry: dict[str, frozenset[str]] = {}
        self._descendants: dict[frozenset[str], frozenset[str]] = {}

        for key, bases in hierarchy.items():
            if not isinstance(key, str):
                continue
            node = self.resolve(key) or key
            bucket = self._edges.setdefault(node, set())
            if not isinstance(bases, (list, tuple)):
                continue
            for base in bases:
                if isinstance(base, str):
                    bucket.add(self.resolve(base) or base)

        collected: dict[str, set[str]] = {}
        for test_id, classes in touched.items():
            if not isinstance(test_id, str) or not isinstance(classes, (list, tuple)):
                continue
            for spelling in classes:
                if not isinstance(spelling, str):
                    continue
                node = self.resolve(spelling) or spelling
                collected.setdefault(node, set()).add(str(test_id))
        self.touched = {node: frozenset(ids) for node, ids in collected.items()}

        self.nodes = frozenset(self.spans) | frozenset(self._edges) | frozenset(
            self.touched
        )

    # -- resolution ------------------------------------------------------

    def resolve(self, spelling: str) -> str | None:
        """Canonical node for ``spelling``: known form, else a unique class path."""
        canonical = self._alias.get(spelling)
        if canonical is not None:
            return canonical
        parts = spelling.split(".")
        for index in range(1, len(parts)):
            target = self._unique_paths.get(".".join(parts[index:]))
            if target is not None:
                return target
        return None

    def mentioned(self, node: str) -> bool:
        """True when the class maps actually know ``node``.

        A class can sit in the symbol table without ever appearing in
        ``classes.json.gz`` (no test constructs it and no hierarchy entry names
        it); such a class is *foreign* to L4 and the caller keeps L1.
        """
        return node in self.touched or node in self._edges

    # -- hierarchy -------------------------------------------------------

    def ancestry(self, node: str) -> frozenset[str]:
        """``node`` plus every class it inherits from (transitively)."""
        cached = self._ancestry.get(node)
        if cached is not None:
            return cached
        found = {node}
        stack = [node]
        while stack:
            for base in self._edges.get(stack.pop(), ()):
                if base not in found:
                    found.add(base)
                    stack.append(base)
        result = frozenset(found)
        self._ancestry[node] = result
        return result

    def descendants(self, targets: Iterable[str]) -> frozenset[str]:
        """``targets`` plus every node inheriting from any of them."""
        key = frozenset(targets)
        cached = self._descendants.get(key)
        if cached is not None:
            return cached
        found = {node for node in self.nodes if self.ancestry(node) & key}
        result = frozenset(found | key)
        self._descendants[key] = result
        return result


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
        self._test_id_set: frozenset[str] | None = None

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
        self._dialects_raw: dict | None = None
        self._keyreads_raw: dict | None = None
        self._testimports_raw: dict | None = None
        self._classes_raw: dict | None = None
        self._usage_style_raw: dict | None = None
        self._classes_ok: bool | None = None
        self._class_graph_cache: _ClassGraph | None = None
        self._class_graph_built = False
        self._l4_noted = False
        self._usage_style_present = (self.root / "usage_style.json.gz").is_file()
        self._shared: frozenset[str] | None = None
        self._tar: tarfile.TarFile | None = None
        self._tar_unavailable = False
        self._source_cache: dict[str, bytes | None] = {}
        self._text_cache: dict[str, str | None] = {}
        self._enum_index = None
        self._enum_flips: dict[str, frozenset[int]] | None = None
        self._enum_flips_loaded = False
        self._assignments_cache: dict[str, tuple[tuple[int, int, tuple[str, ...]], ...]] = {}
        self._definition_lines_cache: dict[str, dict[int, frozenset[str]]] = {}
        self._import_spans_cache: dict[str, tuple[tuple[int, int], ...]] = {}
        self._literal_entries_cache: dict[
            str, tuple[tuple[int, int, str, str, str | None, int | None, bool], ...]
        ] = {}
        self._exposure_cache: dict[str, frozenset[str]] = {}
        self._family_cache: dict[str, frozenset[str]] = {}
        self._inheritance: dict[str, set[str]] | None = None
        self._name_files_cache: dict[str, tuple[str, ...]] | None = None
        self._keyread_index: dict[str, dict[str, list[str]]] | None = None
        self._keyread_reads_cache: dict[str, dict[str, frozenset[str]]] = {}
        self._import_share_cache: dict[str, float | None] = {}

        # L4 is map-gated: report the degraded state as early as possible so the
        # entry point's startup warning log always carries it.
        missing = [name for name in CLASS_MAP_FILES if not (self.root / name).is_file()]
        if missing:
            self._note_l4_disabled(f"{', '.join(missing)} missing")

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

    def _optional_json(self, filename: str, quiet: bool = False) -> dict:
        path = self.root / filename
        try:
            document = _read_json_gz(path)
        except (OSError, ValueError) as error:
            # usage_refs and the v2 maps are optional refinements: a missing
            # file must not even warn (it is the normal, degraded state).
            if not quiet and filename != "usage_refs.json.gz":
                self.warnings.append(f"{filename}: {error}")
            return {}
        if not isinstance(document, dict):
            if not quiet:
                self.warnings.append(f"{filename}: expected a JSON object")
            return {}
        return document

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

    def _dialects(self) -> dict:
        """``dialects.json.gz`` (v2): per-test exposure + file/class scope."""
        if self._dialects_raw is None:
            self._dialects_raw = self._optional_json("dialects.json.gz", quiet=True)
        return self._dialects_raw

    def _keyreads(self) -> dict:
        """``keyreads.json.gz`` (v2): per-test key reads + static usage kinds."""
        if self._keyreads_raw is None:
            self._keyreads_raw = self._optional_json("keyreads.json.gz", quiet=True)
        return self._keyreads_raw

    def _testimports(self) -> dict:
        """``testimports.json.gz`` (v2): test file -> transitive sqlglot modules."""
        if self._testimports_raw is None:
            self._testimports_raw = self._optional_json("testimports.json.gz", quiet=True)
        return self._testimports_raw

    def _classes(self) -> dict:
        """``classes.json.gz`` (L4): per-test constructed classes + hierarchy."""
        if self._classes_raw is None:
            self._classes_raw = self._optional_json("classes.json.gz", quiet=True)
        return self._classes_raw

    def _usage_style(self) -> dict:
        """``usage_style.json.gz`` (L4): name -> file -> receiver-split lines."""
        if self._usage_style_raw is None:
            self._usage_style_raw = self._optional_json(
                "usage_style.json.gz", quiet=True
            )
        return self._usage_style_raw

    def enum_flip_lines(self) -> dict[str, frozenset[int]] | None:
        """``enum_flips.json.gz`` (W1h): file -> proven ``is``/``==`` lines.

        ``None`` means the map is absent (an older, v2/v3 bundle): the caller
        must fall back to building :class:`enum_neutral.EnumIndex` from
        ``source.tar.gz``.  An *empty* dict means the map shipped but lists no
        line, which is a proof of nothing -- never a reason to look at sources.
        """
        if self._enum_flips_loaded:
            return self._enum_flips
        self._enum_flips_loaded = True
        document = self._optional_json("enum_flips.json.gz", quiet=True)
        table = document.get("lines")
        if not isinstance(table, dict):
            self._enum_flips = None
            return None
        lines: dict[str, frozenset[int]] = {}
        for file, numbers in table.items():
            if not isinstance(file, str) or not isinstance(numbers, list):
                continue
            lines[file] = frozenset(
                int(number) for number in numbers if isinstance(number, int)
            )
        self._enum_flips = lines
        return lines

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

    # -- dialects (v2) ---------------------------------------------------

    def has_dialects(self) -> bool:
        return bool(self._dialects().get("exposure"))

    def _dialect_exposure(self) -> dict:
        exposure = self._dialects().get("exposure")
        return exposure if isinstance(exposure, dict) else {}

    def dialect_file_scope(self, file: str) -> str | None:
        scope = self._dialects().get("file_scope")
        if not isinstance(scope, dict):
            return None
        value = scope.get(file)
        return str(value) if isinstance(value, str) else None

    def dialect_class_scope(self, file: str, qualname: str) -> str | None:
        """Class scope for ``qualname``, walking outer classes (``A.B`` -> ``A``)."""
        scope = self._dialects().get("class_scope")
        if not isinstance(scope, dict):
            return None
        classes = scope.get(file)
        if not isinstance(classes, dict):
            return None
        candidate = qualname
        while candidate:
            value = classes.get(candidate)
            if isinstance(value, str):
                return value
            candidate = candidate.rpartition(".")[0]
        return None

    def _inheritance_edges(self) -> dict[str, set[str]]:
        """``{base dialect key: {derived dialect key, ...}}`` from the pin.

        Derived statically from the pinned sources: a test that resolves
        ``spark`` exercises ``Hive`` code through the class hierarchy without
        ever recording ``hive`` in its exposure, so dialect narrowing must keep
        derived dialects in the family.
        """
        scope = self._dialects().get("file_scope")
        if not isinstance(scope, dict):
            return {}
        owner_by_class: dict[str, set[str]] = {}
        for file, key in scope.items():
            if not isinstance(key, str):
                continue
            for symbol in self.symbols_of(file) or ():
                if symbol.kind == "class" and "." not in symbol.qualname:
                    owner_by_class.setdefault(symbol.qualname, set()).add(key)
        edges: dict[str, set[str]] = {}
        for file, key in scope.items():
            if not isinstance(key, str):
                continue
            text = self.source_text(file)
            if not text:
                continue
            for base in _base_classes(text):
                for owner in owner_by_class.get(base, ()):
                    if owner != key:
                        edges.setdefault(owner, set()).add(key)
        return edges

    def dialect_family(self, dialect: str) -> frozenset[str]:
        """``dialect`` plus every dialect key that inherits from it (transitive)."""
        cached = self._family_cache.get(dialect)
        if cached is not None:
            return cached
        if self._inheritance is None:
            self._inheritance = self._inheritance_edges()
        family = {dialect}
        frontier = [dialect]
        while frontier:
            current = frontier.pop()
            for child in self._inheritance.get(current, ()):
                if child not in family:
                    family.add(child)
                    frontier.append(child)
        result = frozenset(family)
        self._family_cache[dialect] = result
        return result

    def tests_exposed_to_family(self, dialect: str) -> frozenset[str] | None:
        """Tests exposed to ``dialect`` or to any dialect derived from it."""
        family = self.dialect_family(dialect)
        allowed: set[str] = set()
        seen_map = False
        for member in family:
            exposed = self.tests_exposed_to(member)
            if exposed is None:
                continue
            seen_map = True
            allowed.update(exposed)
        return frozenset(allowed) if seen_map else None

    def tests_exposed_to(self, dialect: str) -> frozenset[str] | None:
        """Test ids whose recorded dialect exposure includes ``dialect``.

        Tests with no recorded exposure are treated as ``{"default"}``, so a
        narrowly-exposed test can never be dropped by that accident.  ``None``
        when the map is missing *or* only covers part of the committed test
        index (a partial trace must not silently drop the untraced tests).
        """
        exposure = self._dialect_exposure()
        if not exposure:
            return None
        if len(exposure) * 10 < len(self._test_ids) * 9:
            self._note_partial_dialects()
            return None
        warmed = self._exposure_cache.get(dialect)
        if warmed is not None:
            return warmed
        wanted = {dialect}
        matched: set[str] = set()
        for test, dialects in exposure.items():
            if wanted & {str(item) for item in (dialects or ())}:
                matched.add(str(test))
        if dialect == "default":
            # Tests absent from the map never resolved a dialect: "default".
            matched.update(self._test_ids_set() - {str(test) for test in exposure})
        result = frozenset(matched)
        self._exposure_cache[dialect] = result
        return result

    def _test_ids_set(self) -> frozenset[str]:
        if self._test_id_set is None:
            self._test_id_set = frozenset(self._test_ids)
        return self._test_id_set

    def _note_partial_dialects(self) -> None:
        if getattr(self, "_partial_dialects_noted", False):
            return
        self._partial_dialects_noted = True
        self.warnings.append(
            "dialects.json.gz covers a subset of tests_index.json: "
            "dialect narrowing disabled"
        )

    # -- key reads (v2) --------------------------------------------------

    def has_keyreads(self) -> bool:
        return bool(self._keyreads().get("usage_kind"))

    def name_usage_kind(self, file: str, name: str) -> str | None:
        kinds = self._keyreads().get("usage_kind")
        if not isinstance(kinds, dict):
            return None
        table = kinds.get(file)
        if not isinstance(table, dict):
            return None
        kind = table.get(name)
        return str(kind) if isinstance(kind, str) else None

    def name_definition_files(self, name: str) -> tuple[str, ...]:
        """Files whose ``usage_kind`` table names ``name`` (i.e. define it)."""
        kinds = self._keyreads().get("usage_kind")
        if not isinstance(kinds, dict):
            return ()
        if self._name_files_cache is None:
            index: dict[str, list[str]] = {}
            for file, table in kinds.items():
                if not isinstance(table, dict):
                    continue
                for defined in table:
                    index.setdefault(str(defined), []).append(str(file))
            self._name_files_cache = {key: tuple(value) for key, value in index.items()}
        return self._name_files_cache.get(name, ())

    def name_is_fully_keyable(self, name: str) -> bool:
        """True when every defining file classifies ``name`` as ``keyable``."""
        files = self.name_definition_files(name)
        if not files:
            return False
        for file in files:
            if self.name_usage_kind(file, name) != "keyable":
                return False
        return True

    def _build_keyread_index(self) -> dict[str, dict[str, list[str]]]:
        index: dict[str, dict[str, list[str]]] = {}
        exposure = self._keyreads().get("exposure")
        if not isinstance(exposure, dict):
            return index
        for test_id, pairs in exposure.items():
            if not isinstance(pairs, list):
                continue
            for pair in pairs:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    continue
                name = str(pair[0])
                key = pair[1] if isinstance(pair[1], str) else str(pair[1])
                entry = index.get(name)
                if entry is None:
                    entry = index[name] = {}
                bucket = entry.get(key)
                if bucket is None:
                    entry[key] = [str(test_id)]
                else:
                    bucket.append(str(test_id))
        return index

    def keyread_reads(self, name: str) -> dict[str, frozenset[str]]:
        """``{KEY: test ids}`` for every recorded read of ``name``."""
        cached = self._keyread_reads_cache.get(name)
        if cached is not None:
            return cached
        if self._keyread_index is None:
            self._keyread_index = self._build_keyread_index()
        raw = self._keyread_index.get(name, {})
        reads = {key: frozenset(ids) for key, ids in raw.items()}
        self._keyread_reads_cache[name] = reads
        return reads

    # -- test imports (v2) -----------------------------------------------

    def test_file_count(self) -> int:
        table = self._testimports()
        return len(table) if isinstance(table, dict) else 0

    def module_import_share(self, module: str) -> float | None:
        """Fraction of test files whose transitive top-level imports reach ``module``."""
        cached = self._import_share_cache.get(module)
        if cached is not None or module in self._import_share_cache:
            return cached
        table = self._testimports()
        if not isinstance(table, dict) or not table:
            self._import_share_cache[module] = None
            return None
        hits = 0
        for modules in table.values():
            if isinstance(modules, list) and module in modules:
                hits += 1
        share = hits / len(table)
        self._import_share_cache[module] = share
        return share

    # -- class attributes (L4) -------------------------------------------

    def _note_l4_disabled(self, reason: str) -> None:
        """Record (once) that the L4 class-attribute rule is unavailable."""
        if self._l4_noted:
            return
        self._l4_noted = True
        self.warnings.append(
            f"{reason}: L4 class-attribute narrowing disabled"
        )

    def has_classes(self) -> bool:
        """True when the class maps can carry L4.

        A construction trace that misses tests would silently drop the tests
        that build the mutated class, so a trace covering less than 90% of the
        committed test index disables the rule exactly like a missing file
        (MAPS_SPEC: the dialect-narrowing degrade discipline).
        """
        if self._classes_ok is not None:
            return self._classes_ok
        document = self._classes()
        touched = document.get("touched")
        hierarchy = document.get("hierarchy")
        ok = bool(
            isinstance(touched, dict)
            and touched
            and isinstance(hierarchy, dict)
            and self._usage_style_present
            and len(touched) * 10 >= len(self._test_ids) * CLASS_COVERAGE_SHARE
        )
        if not ok:
            self._note_l4_disabled("classes.json.gz unusable or partial")
        self._classes_ok = ok
        return ok

    def _class_graph(self) -> _ClassGraph | None:
        """Build (once) the spellings/hierarchy/construction graph, or ``None``."""
        if self._class_graph_built:
            return self._class_graph_cache
        self._class_graph_built = True
        document = self._classes()
        touched = document.get("touched")
        hierarchy = document.get("hierarchy")
        if not isinstance(touched, dict) or not isinstance(hierarchy, dict):
            return None

        spans: dict[str, tuple[str, int, int]] = {}
        alias: dict[str, str] = {}
        paths: dict[str, set[str]] = {}
        for file, entries in self._symbols().items():
            if not isinstance(entries, list):
                continue
            modules = _class_module_names(str(file))
            if not modules:
                continue
            for entry in entries:
                try:
                    qualname, start, end, kind = entry
                except (TypeError, ValueError):
                    continue
                if str(kind) != "class":
                    continue
                canonical = f"{modules[0]}.{qualname}"
                spans.setdefault(canonical, (str(file), int(start), int(end)))
                for module in modules:
                    alias.setdefault(f"{module}.{qualname}", canonical)
                paths.setdefault(str(qualname), set()).add(canonical)

        # A bare class path is only an alias when exactly one class owns it;
        # ``Tokenizer`` and friends stay module-qualified.
        unique = {
            path: next(iter(found))
            for path, found in paths.items()
            if len(found) == 1
        }
        self._class_graph_cache = _ClassGraph(hierarchy, touched, spans, alias, unique)
        return self._class_graph_cache

    def usage_style(self, name: str) -> dict:
        """``{file: {"self_lines": [...], "class_lines": [...], "instance_lines": [...]}}``.

        The 3-way split is the W1f format.  The predecessor shipped a 2-way
        split (``self_lines`` / ``other_lines``), where ``other_lines`` is
        exactly ``class_lines | instance_lines``; reading it as the
        ``class_lines`` bucket keeps that degraded map recall-safe (the
        conservative union then still contains every instance read).
        """
        entry = self._usage_style().get(name)
        if not isinstance(entry, dict):
            return {}
        result: dict[str, dict] = {}
        for file, detail in entry.items():
            if isinstance(detail, dict):
                result[str(file)] = detail
        return result

    def _ids_for_ordinals(self, ordinals: Iterable[int]) -> set[str]:
        ids = self._test_ids
        count = len(ids)
        return {ids[ordinal] for ordinal in ordinals if 0 <= ordinal < count}

    # -- class families (L4 universe) ------------------------------------

    def _dialect_class_key(self, file: str, qualname: str) -> str | None:
        """Dialect key of the *class itself* (``None`` for nested non-dialects).

        Deliberately exact: unlike :meth:`dialect_class_scope` this does not walk
        outer classes, because a nested ``class Tokenizer`` inside
        ``class Snowflake(Dialect)`` is a tokenizer body (its own instance
        universe), not a dialect class body.
        """
        scope = self._dialects().get("class_scope")
        if not isinstance(scope, dict):
            return None
        classes = scope.get(file)
        if not isinstance(classes, dict):
            return None
        value = classes.get(qualname)
        return str(value) if isinstance(value, str) else None

    @staticmethod
    def _class_nodes(graph: "_ClassGraph", file: str, qualname: str) -> list[str]:
        """Canonical graph nodes for ``qualname`` defined in ``file``."""
        nodes: list[str] = []
        for spelling in _class_spellings(file, qualname):
            node = graph.resolve(spelling)
            if node is not None and node not in nodes:
                nodes.append(node)
        return nodes

    def _class_family(
        self, graph: "_ClassGraph | None", file: str, qualname: str
    ) -> tuple[str, str | None]:
        """``("expression"|"dialect"|"unknown", dialect key or None)``.

        ``expression``: the class or an ancestor is ``Expr``/``Expression`` (the
        construction trace captures its instances), or the trace built the class
        at least once; ``dialect``: the class is a dialect class (exact
        ``class_scope`` entry, or a ``Dialect`` ancestor per the hierarchy); the
        second element is its dialect key when one is known.  ``unknown``:
        instance creation is not traced.
        """
        nodes = self._class_nodes(graph, file, qualname) if graph is not None else []
        if graph is not None:
            for node in nodes:
                if any(
                    _bare_name(ancestor) in EXPRESSION_ROOTS
                    for ancestor in graph.ancestry(node)
                ):
                    return "expression", None
        key = self._dialect_class_key(file, qualname)
        if key is not None:
            return "dialect", key
        if graph is not None:
            # ``classes.json.gz`` only records ``Expr`` constructions, so a class
            # it saw being built *is* an expression class even when the
            # hierarchy entry is missing (a degraded, constructor-only map).
            if any(node in graph.touched for node in nodes):
                return "expression", None
            for node in nodes:
                if any(
                    _bare_name(ancestor) == DIALECT_ROOT
                    for ancestor in graph.ancestry(node)
                ):
                    # A dialect base class: instances resolve to some derived
                    # dialect key, which the exposure map owns.
                    return "dialect", self.dialect_file_scope(file)
        return "unknown", None

    def class_family(self, file: str, qualname: str) -> tuple[str, str | None]:
        """Public form of :meth:`_class_family` (``unknown`` without class maps)."""
        return self._class_family(self._class_graph(), file, qualname)

    def classattr_tests(
        self,
        file: str,
        qualname: str,
        name: str,
        dialect_k: str = "intersect",
    ) -> set[str] | None:
        """L4: test ids that can observe ``qualname.name`` being changed in ``file``.

        ``None`` means "unresolvable": the class is absent from
        ``classes.json.gz`` (or from the symbol table) and is not a known
        dialect class, so the caller must keep the L1 usage rule for that name.

        The set is the class-family evidence, narrowed by the family universe K
        where one exists, unioned with
          * tests covering a ``class_lines`` reference of ``name`` (statically
            resolved receivers: ``TSQL.WEEK_OFFSET``, bare module reads), and
          * tests covering any symbol span of the class or a descendant
            (a method body may read the attribute without the test constructing
            the class directly).

        K depends on the family (see :meth:`_class_family`):
          * expression -- tests constructing the class or a descendant;
          * dialect -- tests whose exposure includes the class's dialect key or a
            derived dialect, *intersected* with the reader/span evidence above
            (an exposed test that never executes one of those lines cannot
            observe the change; ``dialect_k="union"`` restores the old union);
          * unknown (or a dialect class without a usable exposure universe) --
            no universe; every receiver kind stays in the union (instance
            creation is not traced for Tokenizer/Parser/Generator bodies, and a
            descendant we cannot see may read the attribute on ``self``), so
            nothing is lost.

        When the universe *is* captured, ``self``/``cls``-style reads need no
        separate term: a ``self.NAME`` read runs inside a method of the class or
        a descendant, i.e. inside a symbol span (expression tree) or a test that
        is exposed to the dialect (all derived dialects are in the family).

        Dialect refinement (``dialect_k="intersect"``): the exposure universe is
        narrowed to tests that also execute evidence -- a ``class``/``instance``/
        ``self`` read line of ``name`` or a symbol span of the class/descendant.
        A test that is merely exposed to the dialect but never runs one of those
        lines cannot observe the change.  ``dialect_k="union"`` keeps the old,
        wider uncoupled universe for A/B calibration.
        """
        graph = self._class_graph()
        if graph is None:
            return None
        targets = self._class_nodes(graph, file, qualname)
        if not targets:
            return None
        family, dialect_key = self._class_family(graph, file, qualname)
        if family != "dialect" and not any(graph.mentioned(node) for node in targets):
            # The class sits in the symbol table but the construction trace and
            # hierarchy never mention it: we cannot reason about its
            # descendants, so keep L1.
            return None
        descendants = graph.descendants(targets)

        ids: set[str] = set()
        for node in descendants:
            ids.update(graph.touched.get(node, ()))

        universe: frozenset[str] | None = None
        universe_captured = family == "expression"
        if family == "dialect":
            allowed = (
                self.tests_exposed_to_family(dialect_key)
                if dialect_key is not None
                else None
            )
            if allowed is not None:
                universe = allowed
                universe_captured = True

        spans: dict[str, list[tuple[int, int]]] = {}
        for node in descendants:
            span = graph.spans.get(node)
            if span is not None:
                spans.setdefault(span[0], []).append((span[1], span[2]))
        span_ids: set[str] = set()
        for span_file, ranges in spans.items():
            span_ids.update(self._ids_for_ordinals(self.covering_tests(span_file, ranges)))

        class_ids: set[str] = set()
        reader_ids: set[str] = set()
        for style_file, detail in self.usage_style(name).items():
            lines = _style_lines(detail, "class_lines", "other_lines")
            if lines:
                class_ids.update(
                    self._ids_for_ordinals(self.covering_tests(style_file, lines))
                )
            for bucket in ("instance_lines", "self_lines"):
                extra = _style_lines(detail, bucket)
                if extra:
                    reader_ids.update(
                        self._ids_for_ordinals(self.covering_tests(style_file, extra))
                    )

        ids.update(span_ids)
        ids.update(class_ids)
        if not universe_captured:
            # No captured instance universe: every receiver kind is evidence
            # that a test touches this attribute, so keep all of it.
            ids.update(reader_ids)
        if universe is not None:
            if family == "dialect" and dialect_k != "union":
                # Sound tightening: exposure AND executable evidence.
                ids |= universe & (span_ids | class_ids | reader_ids)
            else:
                ids |= universe
        return ids or None

    # -- pinned source: imports and literal entries ----------------------

    def _pinned_tree(self, file: str) -> ast.AST | None:
        text = self.source_text(file)
        if text is None:
            return None
        try:
            return ast.parse(text)
        except (SyntaxError, ValueError):
            return None

    def import_spans(self, file: str) -> tuple[tuple[int, int], ...]:
        """Module-level import statement spans (empty when unparseable)."""
        cached = self._import_spans_cache.get(file)
        if cached is not None:
            return cached
        tree = self._pinned_tree(file)
        spans = _collect_import_spans(tree) if tree is not None else ()
        self._import_spans_cache[file] = spans
        return spans

    def literal_entries(
        self, file: str
    ) -> tuple[tuple[int, int, str, str, str | None, int | None, bool], ...]:
        """Module/class-body literal entries of the pinned file."""
        cached = self._literal_entries_cache.get(file)
        if cached is not None:
            return cached
        tree = self._pinned_tree(file)
        entries = _collect_literal_entries(tree) if tree is not None else ()
        self._literal_entries_cache[file] = entries
        return entries

    def literal_entry_at(self, file: str, line: int) -> tuple[str, str, str | None, int | None] | None:
        """Innermost literal entry spanning ``line`` -> ``(name, kind, key, index)``.

        ``None`` when the line is not inside an unambiguous dict/list/tuple/set
        entry (L3 then does not apply).  Duplicate dict keys are ambiguous and
        report ``None`` as well.
        """
        best: tuple[int, int, str, str, str | None, int | None, bool] | None = None
        for entry in self.literal_entries(file):
            start, end = entry[0], entry[1]
            if not (start <= line <= end):
                continue
            if best is None or (end - start) < (best[1] - best[0]):
                best = entry
        if best is None:
            return None
        _, _, name, kind, key, index, ambiguous = best
        if ambiguous:
            return None
        return name, kind, key, index

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

    def source_files(self) -> tuple[str, ...]:
        """Every sqlglot source file at the pin (the symbol table's keys)."""
        return tuple(str(name) for name in self._symbols())

    def enum_index(self):
        """Lazily built ``enum_neutral.EnumIndex`` over the pinned sources.

        Construction is cheap (no source is read or parsed here); the actual
        index is filled on the first ``is_neutral`` call, which only happens for
        a hunk that passed the ``enum_neutral.maybe_enum_flip`` text gate.  A
        batch whose commits never flip an ``is``/``==`` spelling therefore never
        builds (or even allocates the file list of) the index, and a batch that
        does need it builds it at most once -- the instance is cached here.

        W1h: this is the **fallback** for a bundle without
        ``maps/enum_flips.json.gz``.  When that map is present,
        :func:`_file_is_neutral` answers from the committed line set and this
        index is never built.
        """
        index = getattr(self, "_enum_index", None)
        if index is None:
            from .enum_neutral import EnumIndex

            index = EnumIndex(self.source_files(), self.source_text)
            self._enum_index = index
        return index

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

    def definition_names_at(self, file: str, line: int) -> frozenset[str]:
        """Names whose module/class-level assignment covers ``line``.

        Definition lines execute at import time, before any test is recording
        coverage, so they are noise in a name's reference set (MAPS_SPEC v2).
        """
        index = self._definition_lines_cache.get(file)
        if index is None:
            index = {}
            text = self.source_text(file)
            if text is not None:
                spans = self._assignments_cache.get(file)
                if spans is None:
                    try:
                        tree = ast.parse(text)
                    except SyntaxError:
                        spans = ()
                    else:
                        spans = _collect_assignments(tree)
                    self._assignments_cache[file] = spans
                for start, end, assigned in spans:
                    names = frozenset(assigned)
                    for covered in range(start, end + 1):
                        existing = index.get(covered)
                        index[covered] = names if existing is None else existing | names
            self._definition_lines_cache[file] = index
        return index.get(line, frozenset())
