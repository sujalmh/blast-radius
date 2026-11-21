"""Selection rules (v1 + the v2 refinements) from MAPS_SPEC.md.

Pure functions over (parsed patch, loaded maps, test index).  Every decision
appends a :class:`RuleHit` so the bench workstream can instrument which rule
fired; nothing here reads a patch-specific lookup table.

Tier E (full suite) is the safety net for every shape we do not model
confidently: binary patches, renames, deletes of sqlglot modules, unknown
source files, hunks past the pinned file end, unknown fixtures/test files,
import-region edits and ``sqlglot/__init__.py``.

The v2 refinements (dialect exposure, key reads, test imports) are optional:
every one of them is skipped when its map file is absent, which degrades to the
unrefined, recall-safe selection instead of failing.

L4 (class-attribute narrowing) refines a *class-body assignment* of ``NAME``
inside class ``C``: for that name, the L1 usage set is replaced by the
class-family universe K, tests covering a ``class_lines`` reference of ``NAME``,
and tests covering a symbol span of ``C`` or a descendant.  K is the expression
construction trace for expression classes, the dialect-exposure family for
dialect classes, and UNKNOWN otherwise -- in which case ``self_lines`` and
``instance_lines`` stay in the union (Tokenizer/Parser/Generator instance
creation is not traced).  Unresolvable classes keep L1; a missing/partial
``classes.json.gz`` disables the rule with a warning.

Three W2f refinements sit on top:

* **enum neutrality** (``Knobs.enum_neutral``): a hunk whose only semantic
  change flips ``is``/``==`` between two proven members of the same sqlglot
  ``Enum`` is neutral, exactly like a comment-only hunk (``enum_neutral.py``);
* **dialect-K intersection** (``Knobs.dialect_k``): the dialect family universe
  is intersected with the attr readers / class-span coverers, so a merely
  dialect-exposed test that never executes the attribute cannot be selected;
* **line-biased symbols** (``Knobs.line_bias``): a method hunk whose changed
  lines are inside one body narrows to the tests covering those exact lines
  (``+/-1`` pad) intersected with the symbol-span tests; signature, insertion,
  deleted-statement-head and empty-coverage shapes fall back to the span.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from . import enum_neutral, neutral
from .maps_io import Maps, Symbol
from .patch import FilePatch, Hunk

SQLGLOT_PREFIX = "sqlglot/"
TESTS_PREFIX = "tests/"
SHARED_BASENAMES = frozenset({"conftest.py", "helpers.py"})
INIT_FILE = "sqlglot/__init__.py"

# A module-level change in a module imported by more than this share of test
# files is unbounded import-graph risk (MAPS_SPEC: "> 80%").
IMPORT_SHARE_TIER_E = 0.8

# L3 needs a proven-alive proxy: at least this many tests read *other* keys.
KEYREAD_ALIVE_TESTS = 5

_MISSING = object()


@dataclass(frozen=True)
class Knobs:
    """Rule variants for calibration (defaults = the shipped configuration)."""

    symbol: str = "innermost"  # "innermost" | "all"
    classbody: str = "usage"  # "usage" | "all" | "off"
    dialect: str = "on"  # "on" | "off"
    keyreads: str = "on"  # "on" | "off"
    l4: str = "on"  # "on" | "off"  (class-attribute narrowing)
    enum_neutral: str = "on"  # "on" | "off"  (is<->== enum-member neutrality)
    dialect_k: str = "intersect"  # "intersect" | "union"  (L4 dialect universe)
    line_bias: str = "on"  # "on" | "off"  (method-hunk line subsetting)

    def normalized(self) -> "Knobs":
        symbol = self.symbol if self.symbol in ("innermost", "all") else "innermost"
        classbody = self.classbody
        if classbody is False or classbody == "off":
            classbody = "off"
        elif classbody not in ("usage", "all"):
            classbody = "usage"
        dialect = "off" if self.dialect is False or self.dialect == "off" else "on"
        keyreads = "off" if self.keyreads is False or self.keyreads == "off" else "on"
        l4 = "off" if self.l4 is False or self.l4 == "off" else "on"
        enum_neutral = (
            "off"
            if self.enum_neutral is False or self.enum_neutral == "off"
            else "on"
        )
        dialect_k = "union" if self.dialect_k in (False, "union") else "intersect"
        line_bias = "off" if self.line_bias is False or self.line_bias == "off" else "on"
        return Knobs(
            symbol, classbody, dialect, keyreads, l4, enum_neutral, dialect_k, line_bias
        )


DEFAULT_KNOBS = Knobs()


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
    # rule name -> ids that rule contributed (after narrowing), for the bench
    # adapter's per-rule provenance.
    by_rule: dict[str, set[str]] = field(default_factory=dict)

    def add(self, ids: Iterable[str], rule: str, path: str = "", detail: str = "") -> int:
        before = len(self.tests)
        materialized = set(ids)
        self.tests.update(materialized)
        self.rules.append(RuleHit(rule, path, detail))
        self.by_rule.setdefault(rule, set()).update(materialized)
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


def _enclosing_classes(symbols: Sequence[Symbol], line: int) -> tuple[Symbol, ...]:
    """Class symbols containing ``line``, innermost first."""
    found = [
        symbol
        for symbol in symbols
        if symbol.kind == "class" and symbol.start <= line <= symbol.end
    ]
    found.sort(key=lambda symbol: (symbol.end - symbol.start, -symbol.start))
    return tuple(found)


def _tier_e(index: TestIndex, reason: str) -> Selection:
    return Selection(set(index.all_ids), [RuleHit("tier_e", detail=reason)], "tier_e")


def _file_is_neutral(file_patch: FilePatch, maps: Maps, knobs: Knobs) -> bool:
    """Per-file neutrality: comment-only, or an enum-member ``is``/``==`` flip."""
    path = file_patch.path
    if path is None:
        return False
    comment_only = neutral.changed_lines_only_comments(file_patch)
    if not path.endswith(".py"):
        return comment_only and neutral.non_python_change_is_harmless(path)
    if file_patch.is_new or file_patch.is_deleted:
        return False
    text = maps.source_text(path)
    if text is None:
        return False
    if comment_only and neutral.is_neutral_file(text, file_patch):
        return True
    if knobs.enum_neutral != "on":
        return False
    if not enum_neutral.maybe_enum_flip(file_patch):
        return False
    # W1h: when the bundle ships the proven line set, the answer is a lookup
    # (no pinned AST is parsed, no index is built).  Only an older bundle
    # without maps/enum_flips.json.gz pays for the lazy runtime index, exactly
    # as before.
    committed = maps.enum_flip_lines()
    if committed is not None:
        return enum_neutral.is_committed_flip(committed, path, file_patch)
    return maps.enum_index().is_neutral(text, file_patch, path)


def _removed_lines(hunk: Hunk) -> list[int]:
    """Old-side line numbers of the hunk's ``-`` lines (the changed lines)."""
    lines: list[int] = []
    cursor = hunk.old_start
    for body in hunk.body:
        if body.startswith("\\"):
            continue
        if body.startswith("+"):
            continue
        if body.startswith("-"):
            lines.append(cursor)
            cursor += 1
            continue
        if body.startswith(" ") or body == "":
            cursor += 1
    return lines


def _removed_texts(hunk: Hunk) -> list[tuple[int, str]]:
    """``(old line number, text)`` for the hunk's ``-`` lines."""
    result: list[tuple[int, str]] = []
    cursor = hunk.old_start
    for body in hunk.body:
        if body.startswith("\\"):
            continue
        if body.startswith("+"):
            continue
        if body.startswith("-"):
            result.append((cursor, body[1:]))
            cursor += 1
            continue
        if body.startswith(" ") or body == "":
            cursor += 1
    return result


def _module_of(path: str) -> str:
    """``sqlglot/a/b.py`` -> ``sqlglot.a.b``; ``.../__init__.py`` -> its package."""
    without_suffix = path[: -len(".py")] if path.endswith(".py") else path
    parts = without_suffix.split("/")
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


_SIGNATURE_PREFIXES = ("@", "def ", "async def ", "class ")


def _unbalanced_brackets(text: str) -> bool:
    """True when ``text`` leaves a bracket/quote open (ignoring a ``#`` tail)."""
    depth = 0
    quote: str | None = None
    escaped = False
    for char in text:
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
            continue
        if char == "#":
            break
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth < 0:
                return True
    return depth != 0 or quote is not None


def _multiline_statement_head(text: str) -> bool:
    """Signature/decorator line, or a statement head that continues below."""
    snippet = text.strip()
    if not snippet:
        return False
    if snippet.startswith(_SIGNATURE_PREFIXES):
        return True
    if snippet.endswith((":", "\\")):
        return True
    return _unbalanced_brackets(snippet)


def _function_body_starts(tree: ast.AST) -> dict[int, int]:
    """``def`` line -> first body-statement line, for every function in a file."""
    starts: dict[int, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            starts.setdefault(node.lineno, node.body[0].lineno)
    return starts


def _line_bias_target(
    hunk: Hunk,
    symbols: Sequence[Symbol],
    removed_by_line: dict[int, str],
    added_texts: Sequence[str],
    body_starts: dict[int, int],
) -> tuple[Symbol, tuple[int, ...]] | None:
    """The innermost function a hunk can be narrowed to, or ``None`` (fallback).

    Every fallback is deliberate and recall-safe: a pure insertion, a
    def/signature/decorator edit, a changed line outside the body, a *deleted*
    multi-line statement head (``if x:`` / open brackets, whose body may no
    longer be reached by the pre-patch coverage), or changed lines that do not
    all sit in one innermost function.

    A *rewritten* statement head (``if a:`` -> ``if b:``) is not a deletion: a
    test that can observe the new body must have executed the very head line we
    narrow to, so it stays line-biased.
    """
    if hunk.old_count == 0 or not removed_by_line:
        return None
    changed = sorted(removed_by_line)
    enclosing: list[Symbol] = []
    for line in changed:
        symbol = _innermost(symbols, line)
        if symbol is None or symbol.kind != "function":
            return None
        enclosing.append(symbol)
    target = enclosing[0]
    if any(
        symbol.qualname != target.qualname or symbol.span != target.span
        for symbol in enclosing[1:]
    ):
        return None
    body_start = body_starts.get(target.start)
    if body_start is None:
        return None  # symbols and pinned AST disagree: stay conservative
    removed_heads = 0
    for line in changed:
        if line < body_start:
            return None  # def / decorator / multi-line signature line
        text = removed_by_line[line]
        if text.strip().startswith(_SIGNATURE_PREFIXES):
            return None
        if _multiline_statement_head(text):
            removed_heads += 1
    added_heads = sum(
        1 for text in added_texts if _multiline_statement_head(text)
    )
    if removed_heads > added_heads:
        return None  # a multi-line statement head was deleted, not rewritten
    return target, tuple(changed)



def _dialect_scope(
    maps: Maps, path: str, symbols: Sequence[Symbol], lines: Iterable[int]
) -> str | None:
    """Dialect key scoping a change on ``lines`` (class scope first, then file)."""
    for line in lines:
        for symbol in _enclosing_classes(symbols, line):
            scope = maps.dialect_class_scope(path, symbol.qualname)
            if scope is not None:
                return scope
    return maps.dialect_file_scope(path)


def _fragment_keys(text: str) -> tuple[tuple[str, str | None], ...]:
    """Parse one patch line as a literal entry: candidate ``(kind, key text)``.

    A line can be read several ways (``"a",`` is a list element *and* a set
    element), so every interpretation is returned and the caller picks the one
    matching the pinned AST.  Empty means the line is not a literal entry and
    the L3 rule does not apply (MAPS_SPEC: "if it does not parse → do not
    apply").  Only constant keys are rendered: enum/attribute keys cannot be
    mapped to the recorded keyreads form without importing sqlglot.
    """
    snippet = text.strip()
    if not snippet:
        return ()
    if snippet.endswith(","):
        snippet = snippet[:-1].strip()
    candidates: list[tuple[str, str | None]] = []
    for wrapper, closing in (("{", "}"), ("[", "]"), ("(", ")")):
        try:
            node = ast.parse(f"{wrapper}{snippet}, {closing}", mode="eval").body
        except SyntaxError:
            continue
        if isinstance(node, ast.Dict) and len(node.keys) == 1 and node.keys[0] is not None:
            candidates.append(("dict", _constant_key(node.keys[0])))
        elif isinstance(node, ast.Set) and len(node.elts) == 1:
            candidates.append(("set", _constant_key(node.elts[0])))
        elif isinstance(node, ast.List) and len(node.elts) == 1:
            candidates.append(("list", None))
        elif isinstance(node, ast.Tuple) and len(node.elts) == 1:
            candidates.append(("tuple", None))
    return tuple(candidates)


def _constant_key(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant):
        value = node.value
        return value if isinstance(value, str) else repr(value)
    return None


def _keyread_collapse(
    maps: Maps,
    path: str,
    symbols: Sequence[Symbol],
    name: str,
    line: int,
    text: str,
    knobs: Knobs,
) -> set[str] | None:
    """L3: collapse one name's L1 selection to its key-readers, or ``None``."""
    if knobs.keyreads != "on" or not maps.has_keyreads():
        return None
    candidates = _fragment_keys(text)
    if not candidates:
        return None
    entry = maps.literal_entry_at(path, line)
    if entry is None:
        return None
    entry_name, entry_kind, entry_key, entry_index = entry
    if entry_name != name:
        return None
    fragment = next(
        (key for kind, key in candidates if kind == entry_kind), _MISSING
    )
    if fragment is _MISSING:
        return None
    if entry_kind in ("dict", "set"):
        # Cross-check the fragment against the pinned AST; a constant key must
        # match exactly, and undefined (enum/attribute) keys stay ambiguous.
        if fragment is None or fragment != entry_key:
            return None
        key = fragment
    else:
        if entry_index is None:
            return None
        key = str(entry_index)

    classes = _enclosing_classes(symbols, line)
    qualified = f"{classes[0].qualname}.{name}" if classes else name
    if not maps.name_is_fully_keyable(qualified):
        return None
    reads = maps.keyread_reads(qualified)
    if not reads:
        return None
    if reads.get(key):
        return None  # the changed key has readers: the mutation is reachable
    alive: set[str] = set()
    for other_key, ids in reads.items():
        if other_key != key:
            alive.update(ids)
    if len(alive) < KEYREAD_ALIVE_TESTS:
        return None
    all_readers: set[str] = set()
    for ids in reads.values():
        all_readers.update(ids)
    return all_readers


def _usage_ids(
    maps: Maps, index: TestIndex, path: str, names: Iterable[str]
) -> dict[str, set[str]]:
    """Test ids covering the reference lines of each name (same + cross file).

    Definition lines are dropped: a module/class-level assignment executes at
    import time, so no test's recorded coverage can reach it (it only adds
    first-importer noise).  This keeps the rule correct whether or not the
    committed ``usage_refs`` map already excludes them.
    """
    result: dict[str, set[str]] = {}
    for name in names:
        queries: dict[str, list[int | tuple[int, int]]] = {}
        for reference in maps.references_of(name):
            if name in maps.definition_names_at(reference.file, reference.line):
                continue
            queries.setdefault(reference.file, []).append((reference.line, reference.line))
        ids: set[str] = set()
        for query_file, lines in queries.items():
            ids.update(index.ids_for_ordinals(maps.covering_tests(query_file, lines)))
        result[name] = ids
    return result


def _classattr_ids(
    maps: Maps,
    symbols: Sequence[Symbol],
    path: str,
    name: str,
    lines: Iterable[int],
    dialect_k: str = "intersect",
) -> tuple[set[str], str] | None:
    """L4: sound narrowing for a class-body attribute assignment of ``name``.

    Returns ``(ids, family label)`` -- the replacement for L1 for that single
    name -- or ``None`` when the change cannot be attributed to a resolvable
    class (module-level line, a class the maps do not know, a dialect class
    with no usable exposure): the caller then keeps L1 unchanged.
    """
    combined: set[str] | None = None
    labels: set[str] = set()
    for line in sorted(lines):
        classes = _enclosing_classes(symbols, line)
        if not classes:
            return None  # module-level: not a class attribute
        qualname = classes[0].qualname
        ids = maps.classattr_tests(path, qualname, name, dialect_k=dialect_k)
        if ids is None:
            return None
        family, key = maps.class_family(path, qualname)
        labels.add(f"{family}:{key}" if key else family)
        combined = ids if combined is None else combined | ids
    if not combined:
        return None
    return combined, "+".join(sorted(labels))


def _apply_sqlglot(
    file_patch: FilePatch,
    path: str,
    maps: Maps,
    index: TestIndex,
    selection: Selection,
    knobs: Knobs,
) -> bool:
    """Apply the SQLGLOT rules.  Returns False when Tier E is required."""
    symbols = maps.symbols_of(path)
    if symbols is None:
        return False
    if path == INIT_FILE:
        # Every test imports the package root; its exports are import-time.
        return False

    file_len = maps.file_length(path)
    ranges: list[tuple[int, int]] = []
    biased: list[tuple[tuple[int, int], tuple[int, ...]]] = []
    broad_lines: set[int] = set()
    module_lines: set[int] = set()
    hunk_lines: list[int] = []
    removed: list[tuple[int, str]] = []
    source_text = maps.source_text(path) if knobs.line_bias == "on" else None
    body_starts: dict[int, int] | None = None
    if source_text is not None:
        try:
            body_starts = _function_body_starts(ast.parse(source_text))
        except (SyntaxError, ValueError):
            body_starts = None

    for hunk in file_patch.hunks:
        if file_len is not None:
            if hunk.old_count:
                last_old_line = hunk.old_start + hunk.old_count - 1
                if hunk.old_start < 1 or last_old_line > file_len:
                    return False
            elif hunk.old_start > file_len:
                return False
        old_lines = hunk.old_lines(file_len)
        hunk_lines.extend(old_lines)
        removed_texts = _removed_texts(hunk)
        removed.extend(removed_texts)
        bias = (
            _line_bias_target(
                hunk, symbols, dict(removed_texts), hunk.added, body_starts
            )
            if body_starts is not None
            else None
        )
        bias_added = False
        for line in old_lines:
            symbol = _innermost(symbols, line)
            if symbol is not None and symbol.kind == "function":
                whole_file = (
                    file_len is not None
                    and symbol.start <= 1
                    and symbol.end >= file_len - 1
                )
                if whole_file:
                    broad_lines.add(line)
                    module_lines.add(line)
                elif (
                    bias is not None
                    and bias[0].qualname == symbol.qualname
                    and bias[0].span == symbol.span
                ):
                    if not bias_added:
                        biased.append((symbol.span, bias[1]))
                        bias_added = True
                elif knobs.symbol == "all":
                    for container in symbols:
                        if container.start <= line <= container.end:
                            ranges.append(container.span)
                else:
                    ranges.append(symbol.span)
            else:
                # Module-level, class-body, or trailing text outside any symbol.
                broad_lines.add(line)
                if symbol is None:
                    module_lines.add(line)

    if not ranges and not broad_lines and not biased:
        return True  # e.g. a pure insertion whose anchors do not exist

    removed_by_line = dict(removed)

    # (d) import region: top-level imports are import-time code for everyone.
    # Only real edits count (the removed lines, plus both anchor lines of a pure
    # insertion): a *context* line that happens to sit next to an import must
    # not turn an ordinary function edit into Tier E.
    import_candidates = list(removed_by_line)
    for hunk in file_patch.hunks:
        if hunk.old_count == 0:
            import_candidates.extend(hunk.old_lines(file_len))
    import_spans = maps.import_spans(path)
    if import_spans and any(
        start <= line <= end for line in import_candidates for start, end in import_spans
    ):
        return False

    # (d) module-level change in a module >80% of test files import.
    if module_lines:
        share = maps.module_import_share(_module_of(path))
        if share is not None and share > IMPORT_SHARE_TIER_E:
            return False

    scope_lines = list(removed_by_line) or list(hunk_lines)
    scope = (
        _dialect_scope(maps, path, symbols, scope_lines)
        if knobs.dialect == "on" and maps.has_dialects()
        else None
    )
    allowed: frozenset[str] | None = None
    if scope is not None:
        allowed = maps.tests_exposed_to_family(scope)
        if allowed is None:
            scope = None

    parts: list[tuple[str, str, set[str]]] = []

    if ranges:
        ids = index.ids_for_ordinals(maps.covering_tests(path, ranges))
        parts.append(("coverage", f"{len(ranges)} ranges", ids))

    if biased:
        ids = set()
        for span, lines in biased:
            span_ids = index.ids_for_ordinals(maps.covering_tests(path, [span]))
            line_ids = index.ids_for_ordinals(
                maps.covering_tests(
                    path, [(max(1, line - 1), line + 1) for line in lines]
                )
            )
            narrowed = line_ids & span_ids
            # Empty line coverage (or an empty intersection) falls back to the
            # whole symbol: a missing map row must never lose a test.
            ids |= narrowed if narrowed else span_ids
        parts.append(("linebias", f"{len(biased)} hunks", ids))

    if broad_lines and knobs.classbody == "all":
        # v1 behaviour: every symbol of the file (kept only as an A/B variant).
        ids = index.ids_for_ordinals(
            maps.covering_tests(path, [symbol.span for symbol in symbols])
        )
        parts.append(("classbody_all", f"{len(symbols)} symbols", ids))

    if broad_lines:
        # Safety union: tests whose coverage includes the changed lines.
        ids = index.ids_for_ordinals(
            maps.covering_tests(path, [(line, line) for line in broad_lines])
        )
        parts.append(("anchor", f"{len(broad_lines)} lines", ids))

    if broad_lines and knobs.classbody != "off":
        assigned: dict[str, set[int]] = {}
        for line in sorted(broad_lines):
            for name in maps.assignments_on_lines(path, {line}):
                assigned.setdefault(name, set()).add(line)

        usage = _usage_ids(maps, index, path, assigned)
        collapses: dict[str, set[str]] = {}
        for name in sorted(assigned):
            chosen: set[str] | None = None
            for line in sorted(assigned[name]):
                text = removed_by_line.get(line)
                if text is None:
                    continue
                candidate = _keyread_collapse(
                    maps, path, symbols, name, line, text, knobs
                )
                if candidate is None:
                    continue
                if chosen is None:
                    chosen = candidate
                elif chosen != candidate:
                    chosen = None  # conflicting collapses: keep L1
                    break
            if chosen is not None:
                collapses[name] = chosen

        classattr: dict[str, tuple[set[str], str]] = {}
        if (
            knobs.l4 == "on"
            and any(_enclosing_classes(symbols, line) for line in broad_lines)
            and maps.has_classes()
        ):
            for name in sorted(assigned):
                if name in collapses:
                    continue
                result = _classattr_ids(
                    maps, symbols, path, name, assigned[name], knobs.dialect_k
                )
                if result is not None:
                    classattr[name] = result

        for name, ids in collapses.items():
            parts.append(("keyread", f"{name}: {len(ids)} readers", ids))
        for name, (ids, family) in classattr.items():
            parts.append(("classattr", f"{name}: {len(ids)} tests [{family}]", ids))

        remaining = [
            name for name in sorted(assigned) if name not in collapses and name not in classattr
        ]
        if remaining or not (collapses or classattr):
            parts.append(
                (
                    "usage",
                    ",".join(remaining) if remaining else "no assigned names",
                    set().union(*(usage[name] for name in remaining)) if remaining else set(),
                )
            )

    if scope is not None and allowed is not None:
        before = set().union(*(ids for _, _, ids in parts)) if parts else set()
        parts = [(rule, detail, ids & allowed) for rule, detail, ids in parts]
        dropped = len(before) - len({test for _, _, ids in parts for test in ids})
        if dropped:
            family = maps.dialect_family(scope)
            label = scope if len(family) == 1 else f"{scope}+{len(family) - 1} derived"
            selection.rules.append(
                RuleHit("dialect", path, f"{label}: -{dropped} tests")
            )

    for rule, detail, ids in parts:
        selection.add(ids, rule, path, detail)
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


def select_commit(
    files: Sequence[FilePatch],
    maps: Maps,
    index: TestIndex,
    knobs: Knobs | None = None,
) -> Selection:
    """Select tests for one parsed commit."""
    if not files:
        return Selection(set(), [RuleHit("empty")], "empty")
    knobs = (knobs or DEFAULT_KNOBS).normalized()

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
    neutral_flags = [
        _file_is_neutral(file_patch, maps, knobs) for file_patch in files
    ]
    if all(neutral_flags):
        return Selection(
            set(),
            [RuleHit("neutral", detail="comment-only or enum-member flip")],
            "neutral",
        )

    selection = Selection()

    for file_patch, harmless in zip(files, neutral_flags):
        path = file_patch.path
        assert path is not None  # checked above
        if file_patch.is_new:
            if path.endswith(".py") and (
                _basename(path) in SHARED_BASENAMES
                or path in maps.shared_files()
                or path.endswith("/conftest.py")
            ):
                return _tier_e(index, f"new shared test support file {path}")
            selection.rules.append(RuleHit("new_file", path))
            continue
        if harmless:
            selection.rules.append(RuleHit("file_neutral", path))
            continue
        if path.startswith(SQLGLOT_PREFIX) and path.endswith(".py"):
            if file_patch.is_deleted:
                return _tier_e(index, f"deleted source file {path}")
            if not _apply_sqlglot(file_patch, path, maps, index, selection, knobs):
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
    for rule, ids in selection.by_rule.items():
        ids &= index.all_ids
    selection.tier = "code" if selection.rules else "empty"
    return selection
