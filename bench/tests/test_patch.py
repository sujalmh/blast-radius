"""Unified-diff parsing/application: the selector's anchor logic rests on this."""

from bench.patch import FileChange, Hunk, apply_hunks, parse_patch


def test_parses_single_file_hunk():
    text = (
        "diff --git a/sqlglot/x.py b/sqlglot/x.py\n"
        "index 111..222 100644\n"
        "--- a/sqlglot/x.py\n"
        "+++ b/sqlglot/x.py\n"
        "@@ -10,3 +10,4 @@ def f():\n"
        " ctx1\n"
        "-old\n"
        "+new\n"
        "+extra\n"
        " ctx2\n"
    )
    changes = parse_patch(text)
    assert len(changes) == 1
    change = changes[0]
    assert change.path == "sqlglot/x.py"
    assert not change.is_new and not change.is_deleted
    assert len(change.hunks) == 1
    hunk = change.hunks[0]
    assert hunk.old_span() == (10, 12)
    assert hunk.padded_span(2) == (8, 14)
    assert hunk.anchor_line() == 10
    assert [(tag, body) for tag, body in hunk.changed_lines()] == [("-", "old"), ("+", "new"), ("+", "extra")]


def test_new_file_and_multi_file():
    text = (
        "diff --git a/sqlglot/new.py b/sqlglot/new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/sqlglot/new.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+A = 1\n"
        "+B = 2\n"
        "diff --git a/tests/fixtures/identity.sql b/tests/fixtures/identity.sql\n"
        "--- a/tests/fixtures/identity.sql\n"
        "+++ b/tests/fixtures/identity.sql\n"
        "@@ -217,7 +217,7 @@ SELECT *\n"
        " SELECT 1\n"
        "-SELECT 2\n"
        "+SELECT 3\n"
    )
    changes = parse_patch(text)
    assert [change.path for change in changes] == ["sqlglot/new.py", "tests/fixtures/identity.sql"]
    assert changes[0].is_new
    assert changes[0].hunks[0].old_span() is None
    assert changes[1].hunks[0].old_span() == (217, 223)


def test_deleted_file():
    text = (
        "diff --git a/sqlglot/gone.py b/sqlglot/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/sqlglot/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-A = 1\n"
        "-B = 2\n"
    )
    (change,) = parse_patch(text)
    assert change.path == "sqlglot/gone.py"
    assert change.is_deleted


def test_empty_patch_yields_nothing():
    assert parse_patch("") == []
    assert parse_patch("\n\n") == []


def test_apply_reconstructs_exactly():
    source = "a\nb\nc\nd\ne\n"
    hunk = Hunk(
        old_start=2,
        old_count=3,
        new_start=2,
        new_count=4,
        lines=(" b", "-c", "+C", "+C2", " d"),
    )
    patched, ok, reason = apply_hunks(source, [hunk])
    assert ok, reason
    assert patched == "a\nb\nC\nC2\nd\ne\n"


def test_apply_insertion_with_zero_old_count():
    source = "a\nb\nc\n"
    hunk = Hunk(old_start=1, old_count=0, new_start=2, new_count=1, lines=("+X",))
    patched, ok, reason = apply_hunks(source, [hunk])
    assert ok, reason
    assert patched == "a\nX\nb\nc\n"


def test_apply_reports_context_mismatch():
    source = "a\nb\nc\n"
    hunk = Hunk(old_start=2, old_count=1, new_start=2, new_count=1, lines=(" zzz",))
    patched, ok, reason = apply_hunks(source, [hunk])
    assert not ok
    assert "context mismatch" in reason
    assert patched == source
