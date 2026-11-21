"""Comment-only ("neutral") change detection.

A commit is neutral when every hunk only adds or removes blank / ``#`` comment
lines *and* the patched file parses to the same AST as the pinned file.  The
AST half is what catches the trap in the brief: a line that looks like a
comment but actually lives inside a triple-quoted string.

A second, opt-in neutrality axis lives in ``enum_neutral.py``: a hunk whose only
semantic change is an ``is``/``==`` flip between two proven members of one
sqlglot ``Enum`` is neutral too (identity equality).  ``select`` passes its
``EnumIndex`` here; without one only the comment rule applies.

Reconstruction is in memory only; nothing is written to disk.
"""

from __future__ import annotations

import ast
import re
from typing import Callable, Iterable

from .patch import FilePatch

_CODING = re.compile(r"coding[:=]")


class NeutralityError(ValueError):
    """A hunk could not be applied to the pinned source."""


def changed_lines_only_comments(file_patch: FilePatch) -> bool:
    """True when every added/removed line is blank or starts with ``#``."""
    for hunk in file_patch.hunks:
        for line in hunk.added + hunk.removed:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return False
    return True


def reconstruct(pinned_text: str, file_patch: FilePatch) -> str:
    """Apply ``file_patch``'s hunks to ``pinned_text`` (old side positions).

    Context lines are verified against the pinned source too, so a patch that
    does not apply is rejected instead of silently corrupting the result.
    """
    source = pinned_text.splitlines()
    result: list[str] = []
    cursor = 0

    for hunk in file_patch.hunks:
        # For a pure insertion the hunk anchors *after* old line old_start.
        old_position = hunk.old_start - 1 if hunk.old_count else hunk.old_start
        if old_position < cursor:
            raise NeutralityError("overlapping hunks")
        if old_position > len(source):
            raise NeutralityError(
                f"hunk -{hunk.old_start},{hunk.old_count} is outside the pinned file"
            )
        result.extend(source[cursor:old_position])
        cursor = old_position

        for line in hunk.body:
            if line.startswith("\\"):
                continue
            if line.startswith("+"):
                result.append(line[1:])
                continue
            if line.startswith("-"):
                content = line[1:]
                removed = True
            elif line.startswith(" "):
                content = line[1:]
                removed = False
            else:  # bare blank body line: treat as context
                content = line
                removed = False
            if cursor >= len(source) or source[cursor] != content:
                raise NeutralityError(
                    f"hunk -{hunk.old_start},{hunk.old_count} does not apply to the pinned file"
                )
            cursor += 1
            if not removed:
                result.append(content)

    result.extend(source[cursor:])
    return "\n".join(result)


def _coding_declaration_first_lines(text: str) -> bool:
    for line in text.splitlines()[:2]:
        if _CODING.search(line):
            return True
    return False


def non_python_change_is_harmless(path: str) -> bool:
    """``#`` lines in docs/README are harmless; fixture ``#`` lines are not.

    sqlglot's optimizer fixtures use ``# title:`` / ``# execute:`` directives,
    so a "comment-only" edit to a file under ``tests/`` can change behaviour.
    """
    return not (path == "tests" or path.startswith("tests/"))


def _ast_equal(left: object, right: object) -> bool:
    """Structural AST equality, ignoring line/column attributes."""
    if type(left) is not type(right):
        return False
    if isinstance(left, ast.AST):
        for field_name in left._fields:
            if not _ast_equal(
                getattr(left, field_name, None), getattr(right, field_name, None)
            ):
                return False
        return True
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(
            _ast_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def is_neutral_file(pinned_text: str, file_patch: FilePatch) -> bool:
    """True when this file's hunks are comment-only and AST-preserving."""
    if not changed_lines_only_comments(file_patch):
        return False
    try:
        patched_text = reconstruct(pinned_text, file_patch)
    except NeutralityError:
        return False
    if _coding_declaration_first_lines(patched_text):
        return False
    try:
        pinned_tree = ast.parse(pinned_text)
        patched_tree = ast.parse(patched_text)
    except (SyntaxError, ValueError):
        return False
    return _ast_equal(pinned_tree, patched_tree)


def commit_is_neutral(
    files: Iterable[FilePatch],
    source_of: Callable[[str], bytes | None],
    enum_index: object | None = None,
) -> bool:
    """True when the whole commit is comment-only or enum-flip-only.

    ``source_of`` returns the pinned bytes for a repo-relative path (or None).
    Non-Python files only get the changed-line test: a ``#`` line in a SQL
    fixture or a doc is harmless, and there is no AST to compare.  When an
    ``enum_index`` is supplied (``blastradius.enum_neutral.EnumIndex``), a
    hunk whose only semantic change is an enum-member ``is``/``==`` flip is
    neutral too.
    """
    files = list(files)
    if not files:
        return True
    for file_patch in files:
        path = file_patch.path
        if path is None:
            return False
        comment_only = changed_lines_only_comments(file_patch)
        if not path.endswith(".py"):
            # Fixture files can interpret '#' lines as directives; only
            # non-test assets are assumed comment-neutral.
            if not (comment_only and non_python_change_is_harmless(path)):
                return False
            continue
        if file_patch.is_new or file_patch.is_deleted:
            return False
        data = source_of(path)
        if data is None:
            return False
        try:
            pinned_text = data.decode("utf-8")
        except UnicodeDecodeError:
            return False
        if comment_only and is_neutral_file(pinned_text, file_patch):
            continue
        if enum_index is not None and enum_index.is_neutral(  # type: ignore[attr-defined]
            pinned_text, file_patch, path
        ):
            continue
        return False
    return True
