"""Reference implementation of the MAPS_SPEC v1 selection rules, with knobs.

This is the calibration engine. It exists so `bench/calibrate.py` can A/B rule
variants against `sample/answers.json` before (and after) `blastradius` lands.
`bench/adapter.py` prefers W2's package when it exposes a compatible API and
falls back to this engine otherwise.

Every rule records which test ids it contributed under `Selection.rules`, which
is what `calibrate.py` reports as provenance.
"""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass, field, replace

from .mapio import MapSet
from .patch import FileChange, Hunk, apply_patch, parse_patch

RULES = (
    "neutral",
    "tier_e",
    "fixture",
    "testpy",
    "symbol",
    "line",
    "classbody",
    "usage",
    "anchor",
)

WORKER_COST_MILLISECONDS = 2_000
MAXIMUM_WORKERS = 8


@dataclass(frozen=True)
class Knobs:
    """The rule knobs calibrate.py exposes on the command line."""

    symbol_vs_line: str = "symbol"  # "symbol" | "line"
    classbody: bool = True
    usage: bool = True
    neutral: bool = True
    pad: int = 0
    tier_e: str = "full"  # "full" | "empty"

    def __post_init__(self) -> None:
        if self.symbol_vs_line not in ("symbol", "line"):
            raise ValueError(f"symbol_vs_line must be symbol|line, got {self.symbol_vs_line!r}")
        if self.tier_e not in ("full", "empty"):
            raise ValueError(f"tier_e must be full|empty, got {self.tier_e!r}")
        if self.pad < 0:
            raise ValueError("pad must be >= 0")

    def describe(self) -> str:
        return (
            f"{self.symbol_vs_line} coverage, classbody={'on' if self.classbody else 'off'}, "
            f"usage={'on' if self.usage else 'off'}, neutral={'on' if self.neutral else 'off'}, "
            f"pad={self.pad}, tier-e={self.tier_e}"
        )

    def as_dict(self) -> dict:
        return {
            "symbol_vs_line": self.symbol_vs_line,
            "classbody": self.classbody,
            "usage": self.usage,
            "neutral": self.neutral,
            "pad": self.pad,
            "tier_e": self.tier_e,
        }

    def with_overrides(self, **overrides) -> "Knobs":
        return replace(self, **{key: value for key, value in overrides.items() if value is not None})


@dataclass
class Selection:
    """Result of applying the rules to one patch."""

    tests: set[str] = field(default_factory=set)
    tier_e: bool = False
    rules: dict[str, set[str]] = field(default_factory=dict)
    neutral: bool | None = None
    notes: list[str] = field(default_factory=list)

    def add(self, rule: str, tests: set[str]) -> None:
        if not tests:
            return
        self.rules.setdefault(rule, set()).update(tests)
        self.tests.update(tests)

    def contributors(self, test_id: str) -> list[str]:
        return [rule for rule, tests in self.rules.items() if test_id in tests]

    def rule_pairs(self) -> dict[str, set[str]]:
        return {rule: set(tests) for rule, tests in self.rules.items()}


def partition(path: str) -> str:
    """MAPS_SPEC file partition."""
    if path.startswith("sqlglot/") and path.endswith(".py"):
        return "sqlglot"
    if path.startswith("tests/") and path.endswith(".py"):
        return "testpy"
    if path.startswith("tests/"):
        return "fixture"
    return "other"


def _comment_or_blank(line: str) -> bool:
    stripped = line.strip()
    return stripped == "" or stripped.startswith("#")


def _coding_declaration(text: str) -> bool:
    return any("coding[:=]" in line for line in text.splitlines()[:2])


class Engine:
    def __init__(self, maps: MapSet, knobs: Knobs | None = None, valid_ids: set[str] | None = None):
        self.maps = maps
        self.knobs = knobs or Knobs()
        self.valid_ids = valid_ids if valid_ids is not None else maps.all_tests()
        self._assigned_cache: dict[str, dict[int, set[str]]] = {}
        self._ast_cache: dict[str, ast.AST | None] = {}

    # ------------------------------------------------------------------ public
    def select(self, patch_text: str) -> Selection:
        selection = Selection()
        changes = parse_patch(patch_text)
        if not changes:
            if patch_text.strip():
                self._tier_e(selection, "unparsable-patch")
            else:
                selection.notes.append("empty patch: nothing to select")
            return self._finalize(selection)
        if any(
            not change.hunks and not (change.is_new or change.is_deleted or change.is_binary)
            for change in changes
        ):
            self._tier_e(selection, "diff header with no parseable hunks")
            return self._finalize(selection)
        if all(change.is_binary for change in changes):
            selection.notes.append("binary-only patch: nothing to select")
            return self._finalize(selection)

        neutral, neutral_note = self._is_neutral(changes)
        if neutral is True:
            selection.neutral = True
            selection.rules["neutral"] = set()
            selection.notes.append(neutral_note or "comment-only patch is neutral")
            return self._finalize(selection)
        selection.neutral = neutral
        if neutral is False and self.knobs.neutral:
            selection.notes.append("neutral check rejected")

        for change in changes:
            kind = partition(change.path)
            if kind == "sqlglot":
                self._source_change(change, selection)
            elif kind == "testpy":
                self._test_file_change(change, selection)
            elif kind == "fixture":
                self._fixture_change(change, selection)
            else:
                selection.notes.append(f"ignored non-code file {change.path}")
        return self._finalize(selection)

    # ------------------------------------------------------------- neutrality
    def _is_neutral(self, changes: list[FileChange]) -> tuple[bool | None, str]:
        if not self.knobs.neutral:
            return None, "neutral check disabled"
        if any(change.is_binary for change in changes):
            return False, "binary change"
        for change in changes:
            # Test assets are never neutral: sqlglot fixtures interpret "#"
            # lines as directives (# title: / # dialect: / # execute:), so a
            # comment-only edit under tests/ can change behaviour. Non-test
            # assets (docs, README) have no readers and are harmless.
            if not change.is_python and (change.path == "tests" or change.path.startswith("tests/")):
                return False, f"comment-only change to test asset {change.path} is not neutral"
        for change in changes:
            for tag, text in change.all_changed_lines():
                if not _comment_or_blank(text):
                    return False, f"non-comment change in {change.path}"
        if not changes:
            return None, "no changes"
        # Comment-only. Prove it by AST equality on every changed Python file.
        sources = {
            change.path: self.maps.source_text(change.path)
            for change in changes
            if change.is_python
        }
        if any(source is None for source in sources.values()):
            return None, "comment-only change but pinned source unavailable; not proven neutral"
        patched, problems = apply_patch(
            {path: source for path, source in sources.items() if source is not None},
            [change for change in changes if change.is_python],
        )
        if problems:
            return None, "comment-only change but patch does not apply: " + "; ".join(problems)
        for path, source in sources.items():
            if source is None:
                continue
            new_text = patched.get(path)
            if new_text is None:
                return None, f"comment-only change but {path} could not be reconstructed"
            if _coding_declaration(new_text) and not _coding_declaration(source):
                return False, f"coding declaration introduced in {path}"
            try:
                if ast.dump(ast.parse(source)) != ast.dump(ast.parse(new_text)):
                    return False, f"comment-only lines changed the AST of {path}"
            except SyntaxError:
                return None, f"cannot parse {path} for neutrality check"
        return True, "comment-only patch, AST identical"

    # ------------------------------------------------------------ sqlglot files
    def _source_change(self, change: FileChange, selection: Selection) -> None:
        path = change.path
        if change.is_new:
            selection.notes.append(f"new source file {path}: pure addition selects nothing")
            return
        if change.is_deleted:
            self._tier_e(selection, f"deleted source file {path}")
            return
        if not self.maps.known_source_file(path):
            self._tier_e(selection, f"unknown source file {path}")
            return
        if not change.hunks:
            return
        file_lines = self.maps.file_line_count(path)
        symbols = self.maps.symbol_list(path)
        for hunk in change.hunks:
            span = hunk.padded_span(self.knobs.pad)
            if span is None:
                anchor = hunk.anchor_line()
                if file_lines is not None and anchor > file_lines:
                    self._tier_e(selection, f"insertion anchor beyond EOF of {path}")
                    continue
                self._anchor_change(path, anchor, selection)
                continue
            if file_lines is not None and span[1] > file_lines:
                self._tier_e(selection, f"hunk beyond EOF of {path} (line {span[1]})")
                continue
            self._span_change(path, span, symbols, file_lines, selection)

    def _span_change(
        self,
        path: str,
        span: tuple[int, int],
        symbols,
        file_lines: int | None,
        selection: Selection,
    ) -> None:
        if self.knobs.symbol_vs_line == "line":
            selection.add("line", self.maps.tests_covering_ranges(path, [span]))
            return

        hit_symbols = [symbol for symbol in symbols if symbol.overlaps(*span)]
        if hit_symbols:
            ranges = [(symbol.start, symbol.end) for symbol in hit_symbols]
            selection.add("symbol", self.maps.tests_covering_ranges(path, ranges))
        else:
            selection.notes.append(f"{path}:{span[0]}-{span[1]} intersects no symbol; using exact lines")
            selection.add("symbol", self.maps.tests_covering_ranges(path, [span]))

        whole_file = any(
            (symbol.start <= 1 and (file_lines is None or symbol.end >= file_lines - 1))
            for symbol in hit_symbols
        )
        if whole_file:
            selection.notes.append(f"{path}: change in whole-file symbol treated as module-level")
            self._classbody_expansion(path, span, symbols, selection)
            return
        if not self.knobs.classbody:
            return
        function_spans = [symbol for symbol in symbols if symbol.kind == "function"]
        module_level = [
            line
            for line in range(span[0], span[1] + 1)
            if not any(symbol.start <= line <= symbol.end for symbol in function_spans)
        ]
        if module_level:
            self._classbody_expansion(path, (span[0], span[1]), symbols, selection)

    def _classbody_expansion(self, path: str, span: tuple[int, int], symbols, selection: Selection) -> None:
        selection.add("classbody", self.maps.tests_covering_ranges(path, [(s.start, s.end) for s in symbols]))
        if not self.knobs.usage:
            return
        names = self._assigned_names(path, span)
        usage = self.maps.usage_names(path)
        if names is None:
            selection.notes.append(f"{path}: no pinned source; usage rule falls back to all names")
            names = set(usage)
        lines = sorted({line for name in names for line in usage.get(name, [])})
        selection.add("usage", self.maps.tests_covering_ranges(path, [(line, line) for line in lines]))

    def _anchor_change(self, path: str, anchor: int, selection: Selection) -> None:
        symbols = [symbol for symbol in self.maps.symbol_list(path) if symbol.start <= anchor <= symbol.end]
        if symbols:
            nearest = min(symbols, key=lambda symbol: (symbol.end - symbol.start, symbol.start))
            selection.add("anchor", self.maps.tests_covering_ranges(path, [(nearest.start, nearest.end)]))
        else:
            selection.add("anchor", self.maps.tests_covering_ranges(path, [(anchor, anchor)]))

    # --------------------------------------------------------------- test files
    def _test_file_change(self, change: FileChange, selection: Selection) -> None:
        path = change.path
        if change.is_new:
            self._tier_e(selection, f"new test file {path}")
            return
        if self.maps.is_shared(path):
            self._tier_e(selection, f"shared test support file {path}")
            return
        tests = self.maps.test_file_tests(path)
        if tests is None:
            self._tier_e(selection, f"unknown test file {path}")
            return
        selection.add("testpy", set(tests))

    # ----------------------------------------------------------------- fixtures
    def _fixture_change(self, change: FileChange, selection: Selection) -> None:
        path = change.path
        if change.is_new:
            selection.notes.append(f"new fixture {path}: nothing reads it yet")
            return
        tests = self.maps.fixture_tests(path)
        if tests is None:
            self._tier_e(selection, f"unknown fixture {path}")
            return
        selection.add("fixture", set(tests))

    # ------------------------------------------------------------------ helpers
    def _tier_e(self, selection: Selection, reason: str) -> None:
        selection.tier_e = True
        selection.notes.append(f"tier-e ({self.knobs.tier_e}): {reason}")

    def _finalize(self, selection: Selection) -> Selection:
        if selection.tier_e:
            if self.knobs.tier_e == "full" and self.valid_ids:
                selection.tests = set(self.valid_ids)
                selection.rules = {"tier_e": set(self.valid_ids)}
            else:
                selection.tests = set()
                selection.rules = {"tier_e": set()}
                if self.knobs.tier_e == "full":
                    selection.notes.append("tier-e full requested but no tests_index is loaded")
            return selection
        selection.tests &= self.valid_ids
        for rule, tests in list(selection.rules.items()):
            selection.rules[rule] = tests & self.valid_ids
        return selection

    def _assigned_names(self, path: str, span: tuple[int, int]) -> set[str] | None:
        table = self._assigned_cache.get(path)
        if table is None:
            source = self.maps.source_text(path)
            table = {}
            if source is not None:
                tree = self._parse(path, source)
                if tree is not None:
                    function_spans = [s for s in self.maps.symbol_list(path) if s.kind == "function"]
                    for line, names in _assignments_by_line(tree).items():
                        if any(func.start <= line <= func.end for func in function_spans):
                            continue
                        table.setdefault(line, set()).update(names)
            self._assigned_cache[path] = table
        if not table and self.maps.source_text(path) is None:
            return None
        names: set[str] = set()
        for line in range(span[0], span[1] + 1):
            names.update(table.get(line, ()))
        return names

    def _parse(self, path: str, source: str) -> ast.AST | None:
        if path in self._ast_cache:
            return self._ast_cache[path]
        try:
            tree: ast.AST | None = ast.parse(source)
        except SyntaxError:
            tree = None
        self._ast_cache[path] = tree
        return tree


def _target_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    if isinstance(node, ast.Name):
        names.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for element in node.elts:
            names.update(_target_names(element))
    elif isinstance(node, ast.Starred):
        names.update(_target_names(node.value))
    return names


def _assignments_by_line(tree: ast.AST) -> dict[int, set[str]]:
    """Name -> assignment lines at any scope (function scopes filtered later)."""
    table: dict[int, set[str]] = {}
    for node in ast.walk(tree):
        names: set[str] = set()
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                names.update(_target_names(target))
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
            for item in getattr(node, "items", []):
                if item.optional_vars is not None:
                    names.update(_target_names(item.optional_vars))
            if isinstance(node, (ast.For, ast.AsyncFor)):
                names.update(_target_names(node.target))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        if names:
            table.setdefault(node.lineno, set()).update(names)
    return table


# --------------------------------------------------------------------- planning
def duration_milliseconds(seconds: float) -> int:
    """Same rounding as score.py: floor(seconds * 1000 + 0.5)."""
    return math.floor(seconds * 1000 + 0.5)


def split_tests(
    tests: list[dict],
    selected_ids: set[str],
    max_workers: int = MAXIMUM_WORKERS,
) -> tuple[list[list[int]], int]:
    """LPT split over 1..max_workers, pick min(maxload + 2s x k); ties -> smaller k."""
    by_id = {test["id"]: test for test in tests if test["id"] in selected_ids}
    items = sorted(
        (
            (duration_milliseconds(test["seconds"]), int(test["number"]))
            for test in by_id.values()
        ),
        key=lambda pair: (-pair[0], pair[1]),
    )
    if not items:
        return [], 0

    best_cost: int | None = None
    best_workers: list[list[int]] = []
    for count in range(1, min(max_workers, len(items)) + 1):
        buckets: list[list[int]] = [[] for _ in range(count)]
        loads = [0] * count
        for milliseconds, number in items:
            index = loads.index(min(loads))
            buckets[index].append(number)
            loads[index] += milliseconds
        cost = max(loads) + WORKER_COST_MILLISECONDS * count
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_workers = buckets
    return best_workers, best_cost or 0
