"""Selection rules v1 from MAPS_SPEC.md.

Pure functions over (parsed patch, loaded maps, test index).  Every decision
appends a :class:`RuleHit` so the bench workstream can instrument which rule
fired; nothing here reads a patch-specific lookup table.

Tier E (full suite) is the safety net for every shape we do not model
confidently: binary patches, renames, deletes of sqlglot modules, unknown
source files, hunks past the pinned file end, unknown fixtures/test files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from . import neutral
from .maps_io import Maps, Symbol
from .patch import FilePatch

SQLGLOT_PREFIX = "sqlglot/"
TESTS_PREFIX = "tests/"
SHARED_BASENAMES = frozenset({"conftest.py", "helpers.py"})


@dataclass(frozen=True)
class RuleHit:
    rule: str
    path: str = ""
    detail: str = ""

    def __str__(self) -> str:
        if self.path and self.detail:
            return f"{self.rule}({self.path}: {self.detail})"
        if self.path:
            return f"{self.rule}({self.path})"
        if self.detail:
            return f"{self.rule}({self.detail})"
        return self.rule


@dataclass
class Selection:
    """Selected test ids plus the provenance of the selection."""

    tests: set[str] = field(default_factory=set)
    rules: list[RuleHit] = field(default_factory=list)
    tier: str = "empty"

    def add(self, ids: Iterable[str], rule: str, path: str = "", detail: str = "") -> int:
        before = len(self.tests)
        self.tests.update(ids)
        self.rules.append(RuleHit(rule, path, detail))
        return len(self.tests) - before


class TestIndex:
    """Maps committed test ordinals to the run's test numbers/ids."""

    # pytest would otherwise try to collect this as a test class.
    __test__ = False

    def __init__(
        self,
        ids: Sequence[str],
        number_by_id: dict[str, int],
        tests_by_file: dict[str, list[str]] | None = None,
    ) -> None:
        self.ids = list(ids)
        self.number_by_id = dict(number_by_id)
        self.tests_by_file = tests_by_file or {}
        self._ordinal_ids = list(self.ids)
        self.all_ids = frozenset(number_by_id)

    def ids_for_ordinals(self, ordinals: Iterable[int]) -> set[str]:
        result: set[str] = set()
        count = len(self._ordinal_ids)
        for ordinal in ordinals:
            if 0 <= ordinal < count:
                result.add(self._ordinal_ids[ordinal])
        return result

    def numbers_for_ids(self, ids: Iterable[str]) -> set[int]:
        result: set[int] = set()
        for test_id in ids:
            number = self.number_by_id.get(test_id)
            if number is not None:
                result.add(number)
        return result

    def ids_for_file(self, path: str) -> tuple[str, ...] | None:
        tests = self.tests_by_file.get(path)
        if tests is None:
            return None
        return tuple(tests)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _innermost(symbols: Sequence[Symbol], line: int) -> Symbol | None:
    best: Symbol | None = None
    best_key: tuple[int, int] | None = None
    for symbol in symbols:
        if symbol.start <= line <= symbol.end:
            key = (symbol.end - symbol.start, -symbol.start)
            if best_key is None or key < best_key:
                best = symbol
                best_key = key
    return best


def _tier_e(index: TestIndex, reason: str) -> Selection:
    return Selection(set(index.all_ids), [RuleHit("tier_e", detail=reason)], "tier_e")


def _file_is_neutral(file_patch: FilePatch, maps: Maps) -> bool:
    """Per-file neutrality (used to skip a harmless file in a mixed commit)."""
    if not neutral.changed_lines_only_comments(file_patch):
        return False
    path = file_patch.path
    if path is None:
        return False
    if not path.endswith(".py"):
        return neutral.non_python_change_is_harmless(path)
    if file_patch.is_new or file_patch.is_deleted:
        return False
    text = maps.source_text(path)
    if text is None:
        return False
    return neutral.is_neutral_file(text, file_patch)


def _apply_sqlglot(
    file_patch: FilePatch,
    path: str,
    maps: Maps,
    index: TestIndex,
    selection: Selection,
) -> bool:
    """Apply the SQLGLOT rules.  Returns False when Tier E is required."""
    symbols = maps.symbols_of(path)
    if symbols is None:
        return False

    file_len = maps.file_length(path)
    ranges: list[tuple[int, int]] = []
    broad_lines: set[int] = set()

    for hunk in file_patch.hunks:
        if file_len is not None:
            if hunk.old_count:
                last_old_line = hunk.old_start + hunk.old_count - 1
                if hunk.old_start < 1 or last_old_line > file_len:
                    return False
            elif hunk.old_start > file_len:
                return False
        for line in hunk.old_lines(file_len):
            symbol = _innermost(symbols, line)
            if symbol is not None and symbol.kind == "function":
                whole_file = (
                    file_len is not None
                    and symbol.start <= 1
                    and symbol.end >= file_len - 1
                )
                if whole_file:
                    broad_lines.add(line)
                else:
                    ranges.append(symbol.span)
            else:
                # Module-level, class-body, or trailing text outside any symbol.
                broad_lines.add(line)

    if not ranges and not broad_lines:
        return True  # e.g. a pure insertion whose anchors do not exist

    queries: dict[str, list[int | tuple[int, int]]] = {path: list(ranges)}

    if broad_lines:
        queries[path].extend(symbol.span for symbol in symbols)
        # Safety union: tests whose recorded coverage includes the changed
        # module/class lines themselves (usually import-time, often empty).
        queries[path].extend((line, line) for line in broad_lines)
        names = maps.assignments_on_lines(path, broad_lines)
        for name in names:
            for reference in maps.references_of(name):
                queries.setdefault(reference.file, []).append((reference.line, reference.line))
        selection.rules.append(
            RuleHit("usage", path, ",".join(names) if names else "no assigned names")
        )

    for query_file, file_queries in queries.items():
        ordinals = maps.covering_tests(query_file, file_queries)
        selection.add(
            index.ids_for_ordinals(ordinals),
            "coverage",
            query_file,
            f"{len(file_queries)} ranges -> {len(ordinals)} tests",
        )
    return True


def _apply_testpy(
    file_patch: FilePatch,
    path: str,
    maps: Maps,
    index: TestIndex,
    selection: Selection,
) -> bool:
    if path in maps.shared_files() or _basename(path) in SHARED_BASENAMES:
        return False
    tests = index.ids_for_file(path)
    if tests is None:
        tests = maps.test_file_tests(path)
    if not tests:
        return False
    selection.add(tests, "testpy", path)
    return True


def _apply_fixture(
    file_patch: FilePatch,
    path: str,
    maps: Maps,
    index: TestIndex,
    selection: Selection,
) -> bool:
    readers = maps.fixture_readers(path)
    if readers is None:
        return False
    selection.add(readers, "fixture", path, f"{len(readers)} readers")
    return True


def select_commit(files: Sequence[FilePatch], maps: Maps, index: TestIndex) -> Selection:
    """Select tests for one parsed commit."""
    if not files:
        return Selection(set(), [RuleHit("empty")], "empty")

    # Shapes that neutrality cannot reason about are Tier E regardless of how
    # comment-like their (absent) hunks look.
    for file_patch in files:
        if file_patch.is_binary:
            return _tier_e(index, "binary change")
        if file_patch.is_rename:
            return _tier_e(index, "rename/copy change")
        if file_patch.path is None:
            return _tier_e(index, "file with no usable path")

    # Neutrality is decided per file and reused: comment-only files can be
    # skipped individually in a mixed commit, and a fully neutral commit runs
    # nothing.  This avoids parsing the same file twice.
    neutral_flags = [_file_is_neutral(file_patch, maps) for file_patch in files]
    if all(neutral_flags):
        return Selection(set(), [RuleHit("neutral", detail="comment-only")], "neutral")

    selection = Selection()

    for file_patch, harmless in zip(files, neutral_flags):
        path = file_patch.path
        assert path is not None  # checked above
        if file_patch.is_new:
            selection.rules.append(RuleHit("new_file", path))
            continue
        if harmless:
            selection.rules.append(RuleHit("file_neutral", path))
            continue
        if path.startswith(SQLGLOT_PREFIX) and path.endswith(".py"):
            if file_patch.is_deleted:
                return _tier_e(index, f"deleted source file {path}")
            if not _apply_sqlglot(file_patch, path, maps, index, selection):
                return _tier_e(index, f"unmodelled source change {path}")
        elif path.startswith(TESTS_PREFIX) and path.endswith(".py"):
            if not _apply_testpy(file_patch, path, maps, index, selection):
                return _tier_e(index, f"shared/unknown test file {path}")
        elif path.startswith(TESTS_PREFIX):
            if not _apply_fixture(file_patch, path, maps, index, selection):
                return _tier_e(index, f"unknown fixture {path}")
        elif path in maps.shared_files() or _basename(path) in SHARED_BASENAMES:
            return _tier_e(index, f"shared test support file {path}")
        else:
            # Docs, benchmarks, CI config, README, ...
            selection.rules.append(RuleHit("other", path))

    # "drop tests not in tests.json"
    selection.tests &= index.all_ids
    selection.tier = "code" if selection.rules else "empty"
    return selection
