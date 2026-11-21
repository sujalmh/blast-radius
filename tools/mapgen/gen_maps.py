#!/usr/bin/env python3
"""Build the MAPS_SPEC.md map bundle from a pinned sqlglot checkout.

Inputs are produced by ``run_maps.sh``:

  * ``--collect``  ``pytest --collect-only -q`` output (test ids + order)
  * ``--coverage`` coverage.py sqlite data file, one context per test node id
  * ``--trace``    JSON from ``trace_conftest.py`` (test/module -> files read)
  * ``--dynamic``  JSON from ``trace_conftest.py`` (dialect exposure + key reads)

Outputs (all 0644, deterministic gzip: mtime=0, no filename in the header):

  tests_index.json, coverage.bin.gz, symbols.json.gz, usage.json.gz,
  usage_refs.json.gz, fixtures.json.gz, dialects.json.gz, keyreads.json.gz,
  testimports.json.gz, source.tar.gz, MANIFEST.json

Format details live in MAPS_SPEC.md; this script is the only writer.  It is
stdlib-only except for ``coverage.numbits`` (installed in the mapgen image).
Logs go to stderr.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import io
import json
import os
import sqlite3
import struct
import sys
import tarfile

PINNED_SHA = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
COVERAGE_MAGIC = b"BRcov1"

# Names that are bound at module/class scope and therefore interesting for the
# usage map.  ClassDef/FunctionDef names are covered by symbols instead.
_SKIP_ASSIGN_NAMES = frozenset({"__all__", "__doc__", "__future__"})


def log(message: str) -> None:
    print(f"[mapgen] {message}", file=sys.stderr)


def _coverage_version() -> str:
    try:
        import coverage  # type: ignore

        return str(coverage.__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


# --------------------------------------------------------------------- helpers
def _write_bytes(path: str, payload: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as handle:
        handle.write(payload)
    os.replace(tmp, path)
    os.chmod(path, 0o644)


def _write_gz(path: str, payload: bytes) -> int:
    tmp = path + ".tmp"
    with open(tmp, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
            gz.write(payload)
    os.replace(tmp, path)
    os.chmod(path, 0o644)
    return os.path.getsize(path)


def _json_bytes(payload) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def _rel_under(path: str, root: str, root_real: str | None = None) -> str | None:
    path = os.path.normpath(path)
    if os.path.isabs(path):
        for prefix in (root, root_real):
            if prefix and path.startswith(prefix + os.sep):
                path = path[len(prefix) + 1 :]
                break
        else:
            return None
    if path.startswith("./"):
        path = path[2:]
    return path.replace(os.sep, "/")


def _sqlglot_files(checkout: str) -> list[str]:
    root = os.path.join(checkout, "sqlglot")
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if name.endswith(".py"):
                rel = _rel_under(os.path.join(dirpath, name), checkout)
                if rel is not None:
                    found.append(rel)
    return sorted(found)


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


# ------------------------------------------------------------------- tests list
def load_test_ids(collect_path: str, tests_json: str | None) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    if os.path.isfile(collect_path):
        with open(collect_path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if "::" not in line or not line.startswith("tests/"):
                    continue
                if line in seen:
                    continue
                seen.add(line)
                ids.append(line)
    else:
        log(f"WARNING: collect file {collect_path} is missing")

    if tests_json:
        payload = json.loads(_read_text(tests_json))
        wanted = [entry["id"] for entry in payload.get("tests", [])]
        wanted_set = set(wanted)
        filtered = [test for test in ids if test in wanted_set]
        missing = [test for test in wanted if test not in seen]
        if missing:
            log(
                f"WARNING: {len(missing)} tests from {tests_json} were not collected "
                f"(collection errors?); first: {missing[:5]}"
            )
        extra = [test for test in ids if test not in wanted_set]
        if extra:
            log(f"NOTE: {len(extra)} collected ids are not in tests.json (skipped/unlabeled)")
        ids = filtered
    else:
        log("no --tests-json given: using the raw collection order")
    return ids


def _merge_dynamic(target: dict, payload: dict, include_stats: bool) -> None:
    """Union one trace_conftest dynamic payload into the accumulator."""
    for bucket, names in (payload.get("dialects") or {}).items():
        target["dialects"].setdefault(bucket, set()).update(
            name for name in names if isinstance(name, str)
        )
    for bucket, by_name in (payload.get("keys") or {}).items():
        entry = target["keys"].setdefault(bucket, {})
        for name, keys in by_name.items():
            entry.setdefault(name, set()).update(str(key) for key in keys)
    for name, origin in (payload.get("names") or {}).items():
        target["names"].setdefault(name, origin)
    target["skipped"].update(payload.get("skipped") or ())
    if include_stats:
        # Per-process counters are not additive (workers may split the same
        # test's reads), so only the parent's raw counters are kept; derived
        # numbers are recomputed from the merged data below.
        for key, value in (payload.get("stats") or {}).items():
            if isinstance(value, int):
                target["stats"][key] = value


def load_dynamic(dynamic_path: str) -> tuple[dict, dict[str, int]]:
    """Merge the parent trace and every spawned worker's trace.

    ``trace_conftest.py`` writes ``<BR_DYNAMIC_OUT>`` in the pytest parent and
    ``<BR_DYNAMIC_OUT>.<pid>.child.json`` in each ``ProcessPoolExecutor``
    worker (MAPS_SPEC v2, bug 1: dialect and key-read activity for fixture-driven
    tests happens inside those workers).  The child files are discovered next to
    the parent file so the pipeline keeps a single ``--dynamic`` argument.
    """
    merged: dict = {
        "dialects": {},
        "keys": {},
        "names": {},
        "skipped": set(),
        "stats": {},
    }
    stats = {"parents": 0, "children": 0}
    detail = []
    directory, base = os.path.split(dynamic_path)
    candidates = [dynamic_path]
    if os.path.isdir(directory or "."):
        prefix = base + "."
        for name in sorted(os.listdir(directory or ".")):
            if name.startswith(prefix) and name.endswith(".child.json"):
                candidates.append(os.path.join(directory, name))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        try:
            payload = json.loads(_read_text(candidate))
        except Exception as error:  # noqa: BLE001 - one bad worker must not stop the run
            log(f"WARNING: cannot read dynamic trace {candidate}: {error}")
            continue
        if candidate == dynamic_path:
            stats["parents"] += 1
        else:
            stats["children"] += 1
        _merge_dynamic(merged, payload, include_stats=candidate == dynamic_path)
        detail.append(
            "{}:{}k".format(
                os.path.basename(candidate),
                len(payload.get("keys") or {}),
            )
        )
    merged["skipped"] = sorted(merged["skipped"])
    merged["dialects"] = {
        bucket: sorted(names) for bucket, names in merged["dialects"].items()
    }
    merged["keys"] = {
        bucket: {name: sorted(keys) for name, keys in sorted(by_name.items())}
        for bucket, by_name in merged["keys"].items()
    }
    merged["stats"].update(
        {
            "proxied_names": len(merged["names"]),
            "key_pairs": sum(
                len(keys) for by_name in merged["keys"].values() for keys in by_name.values()
            ),
            "dialect_buckets": len(merged["dialects"]),
            "traces": stats["parents"] + stats["children"],
            "child_traces": stats["children"],
        }
    )
    if detail:
        shown = ", ".join(detail[:5])
        if len(detail) > 5:
            shown += f", ... ({len(detail) - 5} more)"
        log(f"dynamic traces: {shown}")
    return merged, stats


def parse_coverage_payload(payload: bytes) -> dict[str, list[tuple[int, int, int]]]:
    """Decode ``coverage.bin.gz`` into {file: [(start, end, ordinal), ...]}."""
    if len(payload) < 10 or payload[:6] != COVERAGE_MAGIC:
        raise ValueError("coverage.bin.gz: missing BRcov1 magic")
    (nfiles,) = struct.unpack_from(">I", payload, 6)
    cursor = 10
    table: list[tuple[str, int]] = []
    for _ in range(nfiles):
        (name_len,) = struct.unpack_from(">H", payload, cursor)
        cursor += 2
        name = payload[cursor : cursor + name_len].decode("utf-8")
        cursor += name_len
        (offset,) = struct.unpack_from(">Q", payload, cursor)
        cursor += 8
        table.append((name, offset))

    intervals: dict[str, list[tuple[int, int, int]]] = {}
    for name, offset in table:
        (ntests,) = struct.unpack_from(">I", payload, offset)
        offset += 4
        items: list[tuple[int, int, int]] = []
        for _ in range(ntests):
            ordinal, nruns = struct.unpack_from(">II", payload, offset)
            offset += 8
            for _ in range(nruns):
                start, run_len = struct.unpack_from(">II", payload, offset)
                offset += 8
                items.append((start, start + run_len - 1, ordinal))
        intervals[name] = items
    return intervals


def _numbits_to_nums(numbits: bytes) -> list[int]:
    try:
        from coverage.numbits import numbits_to_nums  # type: ignore

        return list(numbits_to_nums(numbits))
    except Exception:  # noqa: BLE001 - fall back to the documented bit layout
        if not numbits:
            return []
        value = int.from_bytes(numbits, "big")
        out: list[int] = []
        line = 1
        while value:
            if value & 1:
                out.append(line)
            value >>= 1
            line += 1
        return out


def _lines_to_runs(lines: list[int]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    for line in sorted(set(lines)):
        if runs and line == runs[-1][0] + runs[-1][1]:
            start, length = runs[-1]
            runs[-1] = (start, length + 1)
        else:
            runs.append((line, 1))
    return runs


def load_coverage_data(
    data_file: str, checkout: str, ordinal_of: dict[str, int]
) -> tuple[dict[str, dict[int, set[int]]], dict[str, int]]:
    stats = {"unknown_contexts": 0, "outside_files": 0, "empty": 0}
    per_file: dict[str, dict[int, set[int]]] = {}
    if not os.path.isfile(data_file):
        log(f"WARNING: coverage data {data_file} is missing; coverage map will be empty")
        return per_file, stats
    root = os.path.normpath(checkout)

    uri = "file:" + os.path.abspath(data_file) + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        files = {fid: path for fid, path in connection.execute("SELECT id, path FROM file")}
        # Resolve once per file: coverage may store /tmp/... while the checkout
        # is /private/tmp/... (macOS symlink), or vice versa.
        rel_by_fid: dict[int, str | None] = {}
        for fid, path in files.items():
            rel = _rel_under(path, root)
            if rel is None:
                rel = _rel_under(os.path.realpath(path), root)
            rel_by_fid[fid] = rel
        contexts = {
            cid: ctx for cid, ctx in connection.execute("SELECT id, context FROM context")
        }
        rows = connection.execute("SELECT file_id, context_id, numbits FROM line_bits")
        for file_id, context_id, numbits in rows:
            context = contexts.get(context_id, "")
            test_id = context.split("|", 1)[0]  # pytest-cov-style phase suffix
            if not test_id:
                stats["empty"] += 1  # import-time / module scope, not a test
                continue
            ordinal = ordinal_of.get(test_id)
            if ordinal is None:
                stats["unknown_contexts"] += 1
                continue
            rel = rel_by_fid.get(file_id)
            if rel is None:
                stats["outside_files"] += 1
                continue
            numbers = _numbits_to_nums(numbits)
            if not numbers:
                continue
            per_file.setdefault(rel, {}).setdefault(ordinal, set()).update(numbers)
    finally:
        connection.close()
    return per_file, stats


def write_coverage(path: str, per_file: dict[str, dict[int, set[int]]]) -> int:
    names = sorted(per_file)
    header = bytearray(COVERAGE_MAGIC)
    header += struct.pack(">I", len(names))
    offsets_size = sum(2 + len(name.encode("utf-8")) + 8 for name in names)
    offset = len(header) + offsets_size

    table = bytearray()
    blocks = bytearray()
    for name in names:
        encoded = name.encode("utf-8")
        table += struct.pack(">H", len(encoded)) + encoded + struct.pack(">Q", offset)
        block = bytearray()
        tests = per_file[name]
        block += struct.pack(">I", len(tests))
        for ordinal in sorted(tests):
            runs = _lines_to_runs(list(tests[ordinal]))
            block += struct.pack(">II", ordinal, len(runs))
            for start, length in runs:
                block += struct.pack(">II", start, length)
        block = bytes(block)
        blocks += block
        offset += len(block)

    return _write_gz(path, bytes(header) + bytes(table) + bytes(blocks))


# ---------------------------------------------------------------------- symbols
class SymbolVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.out: list[list] = []

    def _enter(self, node, kind: str) -> None:
        qualname = ".".join(self.stack + [node.name])
        self.out.append([qualname, node.lineno, node.end_lineno, kind])
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):  # noqa: N802
        self._enter(node, "function")

    def visit_AsyncFunctionDef(self, node):  # noqa: N802
        self._enter(node, "function")

    def visit_ClassDef(self, node):  # noqa: N802
        self._enter(node, "class")


def file_symbols(tree: ast.AST) -> list[list]:
    visitor = SymbolVisitor()
    visitor.visit(tree)
    deduped: dict[tuple, list] = {}
    for entry in visitor.out:
        key = (entry[0], entry[1], entry[2], entry[3])
        deduped[key] = entry
    return [deduped[key] for key in sorted(deduped)]


# ------------------------------------------------------------------------ usage
def _target_names(target, out: set[str]) -> None:
    if isinstance(target, ast.Name):
        out.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            _target_names(element, out)
    elif isinstance(target, ast.Starred):
        _target_names(target.value, out)


def _target_bindings(target, out: dict[str, set[int]]) -> None:
    """Like ``_target_names``, but records the binding (definition) lines."""
    if isinstance(target, ast.Name):
        out.setdefault(target.id, set()).add(target.lineno)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            _target_bindings(element, out)
    elif isinstance(target, ast.Starred):
        _target_bindings(target.value, out)


def assigned_bindings(tree: ast.AST) -> dict[str, set[int]]:
    """Names bound by Assign/AnnAssign at module or class body level -> lines."""
    bindings: dict[str, set[int]] = {}

    def scan(body) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue  # locals are not module/class-level names
            if isinstance(node, ast.ClassDef):
                scan(node.body)
                continue
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    _target_bindings(target, bindings)
                continue
            if isinstance(node, ast.AnnAssign):
                _target_bindings(node.target, bindings)
                continue
            # module/class level control flow: if/try/with/for bodies still bind
            # at the enclosing scope.
            for _field, value in ast.iter_fields(node):
                if isinstance(value, list):
                    statements = [item for item in value if isinstance(item, ast.stmt)]
                    if statements:
                        scan(statements)

    scan(getattr(tree, "body", []))
    return {
        name: lines for name, lines in bindings.items() if name not in _SKIP_ASSIGN_NAMES
    }


def assigned_names(tree: ast.AST) -> set[str]:
    """Names bound by Assign/AnnAssign at module or class body level."""
    return set(assigned_bindings(tree))


def build_usage(trees: dict[str, ast.AST]):
    """Return (usage, crossfile).

    ``usage``     {file: {NAME: [lines in *that file* referencing NAME]}}
    ``crossfile`` {NAME: {file: [lines referencing NAME]}} for names whose
                  references reach a file other than where they are assigned.

    MAPS_SPEC v1 keys usage by the defining file and stores bare line numbers,
    which can only be sound for references in the same file.  ``crossfile`` is
    the unambiguous superset for selectors that want cross-module references;
    it is written to ``usage_refs.json.gz``.

    References are every ``Name.id == NAME`` *and* ``Attribute.attr == NAME``
    occurrence (v2 fix: ``dialect.WEEK_OFFSET`` / ``self.dialect.WEEK_OFFSET``
    reads are not ``Name`` nodes and used to be invisible).  Each name's own
    definition lines are excluded from its reference set: module/class-body
    assignments execute at import time, so keeping them only adds first-importer
    noise (`WEEK_OFFSET = 0` is not a *use* of WEEK_OFFSET).
    """
    references: dict[str, dict[str, set[int]]] = {}
    definitions: dict[str, dict[str, set[int]]] = {}
    for rel, tree in trees.items():
        local: dict[str, set[int]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                local.setdefault(node.id, set()).add(node.lineno)
            elif isinstance(node, ast.Attribute):
                local.setdefault(node.attr, set()).add(node.lineno)
        for name, lines in local.items():
            references.setdefault(name, {})[rel] = lines
        for name, lines in assigned_bindings(tree).items():
            definitions.setdefault(name, {})[rel] = lines

    def reference_lines(name: str, rel: str) -> set[int]:
        lines = references.get(name, {}).get(rel)
        if not lines:
            return set()
        return lines - definitions.get(name, {}).get(rel, set())

    usage: dict[str, dict[str, list[int]]] = {}
    defining: dict[str, set[str]] = {}
    for rel, tree in trees.items():
        names = assigned_names(tree)
        if not names:
            continue
        entry: dict[str, list[int]] = {}
        for name in sorted(names):
            defining.setdefault(name, set()).add(rel)
            entry[name] = sorted(reference_lines(name, rel))
        usage[rel] = entry

    crossfile: dict[str, dict[str, list[int]]] = {}
    for name in sorted(defining):
        by_file: dict[str, list[int]] = {}
        for rel in references.get(name, {}):
            lines = reference_lines(name, rel)
            if lines:
                by_file[rel] = sorted(lines)
        files = defining[name]
        if any(path not in files for path in by_file):
            crossfile[name] = dict(sorted(by_file.items()))
    return usage, crossfile


# -------------------------------------------------------------------- testing
def _test_file_of(test_id: str) -> str:
    return test_id.split("::", 1)[0]


def build_fixtures(
    trace: dict[str, list[str]], test_ids: list[str], checkout: str
) -> tuple[dict, dict[str, int]]:
    by_file: dict[str, list[str]] = {}
    for test_id in test_ids:
        by_file.setdefault(_test_file_of(test_id), []).append(test_id)
    known = set(test_ids)

    fixtures: dict[str, set[str]] = {}
    stats = {"unknown_bucket_reads": 0, "module_bucket_reads": 0, "fixture_files": 0}
    for bucket, paths in trace.items():
        if bucket == "__unknown__":
            stats["unknown_bucket_reads"] += len(paths)
            continue
        if bucket.startswith("module:"):
            tests = by_file.get(bucket[len("module:") :], [])
            stats["module_bucket_reads"] += len(paths)
        else:
            tests = [bucket] if bucket in known else []
        if not tests:
            continue
        for path in paths:
            if path.endswith(".py"):
                continue  # source/import reads are not fixtures
            fixtures.setdefault(path, set()).update(tests)

    payload: dict = {path: sorted(tests) for path, tests in sorted(fixtures.items())}
    payload["__test_files__"] = {path: tests for path, tests in sorted(by_file.items())}
    payload["__shared__"] = find_shared_files(checkout, list(by_file))
    stats["fixture_files"] = len(fixtures)
    return payload, stats


def _module_candidates(checkout: str, dotted: str) -> list[str]:
    base = dotted.replace(".", "/")
    return [base + ".py", base + "/__init__.py"]


def find_shared_files(checkout: str, test_files: list[str]) -> list[str]:
    """Test-support modules imported by two or more test files, plus conftests."""
    importers: dict[str, set[str]] = {}
    for test_file in test_files:
        path = os.path.join(checkout, test_file)
        try:
            tree = ast.parse(_read_text(path))
        except Exception as error:  # noqa: BLE001
            log(f"WARNING: cannot parse {test_file}: {error}")
            continue
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue  # relative imports inside tests/ are uncommon here
                if node.module:
                    modules.add(node.module)
                    modules.update(
                        f"{node.module}.{alias.name}" for alias in node.names if alias.name != "*"
                    )
                else:
                    modules.update(alias.name for alias in node.names)
        for module in modules:
            for candidate in _module_candidates(checkout, module):
                if candidate.startswith("sqlglot/"):
                    continue  # the package under test is not test support
                if os.path.isfile(os.path.join(checkout, candidate)) and candidate.endswith(".py"):
                    importers.setdefault(candidate, set()).add(test_file)

    shared = {path for path, users in importers.items() if len(users) >= 2}
    # conftests and pytest support files are always "shared"
    for dirpath, dirnames, filenames in os.walk(checkout):
        dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", "sqlglot"}]
        for name in filenames:
            if name == "conftest.py":
                rel = _rel_under(os.path.join(dirpath, name), checkout)
                if rel:
                    shared.add(rel)
    return sorted(shared)


# --------------------------------------------------------------------- tarball
def write_source_tarball(path: str, checkout: str, files: list[str]) -> int:
    temporary = path + ".tmp"
    with open(temporary, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for rel in files:
                    full = os.path.join(checkout, rel)
                    data = open(full, "rb").read()
                    info = tarfile.TarInfo(name=rel)
                    info.size = len(data)
                    info.mtime = 0
                    info.mode = 0o644
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    tar.addfile(info, io.BytesIO(data))
    os.replace(temporary, path)
    os.chmod(path, 0o644)
    return os.path.getsize(path)


# --------------------------------------------------------------------- dialects
_DIALECT_DIRS = frozenset({"dialects", "generators", "parsers"})
_NON_DIALECT_MODULES = frozenset({"__init__", "base", "dialect"})

# A reference "reads a key" only through these shapes; anything else (iteration,
# expansion, copy, len(), equality, truthiness, attribute access) cannot be
# observed by a proxy and therefore makes the constant "opaque".
_KEYABLE_CALL_ATTRS = frozenset({"get", "__contains__", "__getitem__"})


def dialect_keys(checkout: str) -> dict[str, str]:
    """{registered key: class name} from sqlglot/dialects/__init__.py."""
    path = os.path.join(checkout, "sqlglot/dialects/__init__.py")
    keys: dict[str, str] = {}
    try:
        tree = ast.parse(_read_text(path))
    except Exception as error:  # noqa: BLE001
        log(f"WARNING: cannot parse {path}: {error}")
        return keys
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "DIALECTS" for target in node.targets
        ):
            try:
                for name in ast.literal_eval(node.value):
                    keys[str(name).lower()] = str(name)
            except Exception as error:  # noqa: BLE001
                log(f"WARNING: cannot read DIALECTS: {error}")
    return keys


def _bucket_expander(by_file: dict[str, list[str]], known: set[str]):
    def expand(bucket: str) -> list[str]:
        if bucket.startswith("module:"):
            return by_file.get(bucket[len("module:") :], [])
        return [bucket] if bucket in known else []

    return expand


def build_dialects(
    dynamic: dict,
    test_ids: list[str],
    by_file: dict[str, list[str]],
    trees: dict[str, ast.AST],
    checkout: str,
) -> tuple[dict, dict[str, int]]:
    """maps/dialects.json.gz: per-test dialect exposure + static file scope."""
    expand = _bucket_expander(by_file, set(test_ids))
    stats = {"exposure_tests": 0, "exposure_names": 0, "default_fallbacks": 0}
    exposure: dict[str, set[str]] = {}
    for bucket, names in (dynamic.get("dialects") or {}).items():
        tests = expand(bucket)
        cleaned = sorted({name for name in names if isinstance(name, str) and name})
        if not tests or not cleaned:
            continue
        for test_id in tests:
            exposure.setdefault(test_id, set()).update(cleaned)

    # A test that never resolved a dialect ran under the base/default dialect
    # implicitly (e.g. tests/test_jsonpath.py); record it explicitly so the
    # selector's dialect narrowing never drops it by accident.
    for test_id in test_ids:
        bucket = exposure.setdefault(test_id, set())
        if not bucket:
            bucket.add("default")
            stats["default_fallbacks"] += 1

    keys = dialect_keys(checkout)
    file_scope: dict[str, str] = {}
    class_scope: dict[str, dict[str, str]] = {}
    for rel, tree in trees.items():
        parts = rel.split("/")
        if len(parts) != 3 or parts[0] != "sqlglot" or parts[1] not in _DIALECT_DIRS:
            continue
        if not parts[2].endswith(".py"):
            continue
        stem = parts[2][: -len(".py")]
        if stem in _NON_DIALECT_MODULES or stem not in keys:
            continue
        file_scope[rel] = stem
        if parts[1] == "dialects":
            classes = {
                node.name: stem
                for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name.lower() in keys
            }
            if classes:
                class_scope[rel] = classes

    stats["exposure_tests"] = len(exposure)
    stats["exposure_names"] = sum(len(names) for names in exposure.values())
    payload = {
        "exposure": {test_id: sorted(names) for test_id, names in sorted(exposure.items())},
        "file_scope": dict(sorted(file_scope.items())),
        # Additive extension (MAPS_SPEC only asks for file_scope): class -> key
        # for dialect classes, so a symbol-level selector can scope
        # `class Snowflake(Dialect)` even when the file also holds helpers.
        "class_scope": dict(sorted(class_scope.items())),
    }
    return payload, stats


# --------------------------------------------------------------------- keyreads
def _parent_maps(tree: ast.AST) -> tuple[dict, dict]:
    parents: dict = {}
    fields: dict = {}
    for node in ast.walk(tree):
        for field, value in ast.iter_fields(node):
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        parents[item] = node
                        fields[item] = field
            elif isinstance(value, ast.AST):
                parents[value] = node
                fields[value] = field
    return parents, fields


def _ref_reason(ref, parents: dict, fields: dict) -> str | None:
    """None when the reference only reads a key/index; else an opaque reason."""
    parent = parents.get(ref)
    if parent is None:
        return "detached"
    field = fields.get(ref)
    if isinstance(parent, ast.Subscript):
        if field == "value":
            return None if isinstance(parent.ctx, ast.Load) else "subscript-store"
        return "subscript-index"
    if isinstance(parent, ast.Attribute) and field == "value":
        if parent.attr in _KEYABLE_CALL_ATTRS:
            grand = parents.get(parent)
            if isinstance(grand, ast.Call) and fields.get(parent) == "func":
                return None
        return "attribute:" + parent.attr
    if isinstance(parent, ast.Call) and field == "func":
        return "call:" + type(parent.func).__name__
    if isinstance(parent, ast.Compare) and field == "comparators":
        return None
    return f"{type(parent).__name__}.{field}"


def _put(store: dict, key, reason: str | None) -> None:
    """Keep the first opaque reason; "" marks "referenced and keyable only"."""
    text = "" if reason is None else reason
    if key not in store:
        store[key] = text
    elif store[key] == "" and text:
        store[key] = text


def _name_bindings(tree: ast.AST) -> set:
    """Name/Attribute nodes that *bind* a name rather than use one.

    ``KEYWORDS = {...}`` in a subclass rebinds the name; that is not a read of
    the inherited constant and must not make the table look "opaque".  A
    ``Subscript`` target (``X[k] = v``) is a read, so target walks stop there.
    """
    bindings: set = set()

    def mark(node) -> None:
        if isinstance(node, (ast.Name, ast.Attribute)):
            bindings.add(node)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for element in node.elts:
                mark(element)
        elif isinstance(node, ast.Starred):
            mark(node.value)

    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets = [node.target]
        elif isinstance(node, ast.comprehension):
            targets = [node.target]
        elif isinstance(node, ast.NamedExpr):
            targets = [node.target]
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            targets = [node.optional_vars]
        for target in targets:
            mark(target)
    return bindings


def _collect_refs(tree: ast.AST, name_store: dict, attr_store: dict, class_store: dict) -> None:
    parents, fields = _parent_maps(tree)
    bindings = _name_bindings(tree)
    stack: list[str] = []

    def visit(node) -> None:
        if isinstance(node, ast.ClassDef):
            for child in list(node.decorator_list) + list(node.bases):
                visit(child)
            stack.append(node.name)
            for child in node.body:
                visit(child)
            stack.pop()
            return
        if isinstance(node, ast.Name):
            if node not in bindings:
                reason = _ref_reason(node, parents, fields)
                _put(name_store, node.id, reason)
                if stack:
                    _put(class_store, (".".join(stack), node.id), reason)
        elif isinstance(node, ast.Attribute):
            if node not in bindings:
                _put(attr_store, node.attr, _ref_reason(node, parents, fields))
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)


def _is_container_expr(node) -> bool:
    if isinstance(node, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id in {"dict", "list", "tuple", "set", "frozenset"}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return True
    return False


def _container_definitions(trees: dict[str, ast.AST]):
    """Yield (file, full name, bare name, class qualname or None)."""
    out: list[tuple[str, str, str, str | None]] = []

    def scan(rel: str, body, prefix: str | None) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(node, ast.ClassDef):
                scan(rel, node.body, f"{prefix}.{node.name}" if prefix else node.name)
                continue
            value = None
            targets = []
            if isinstance(node, ast.Assign):
                value = node.value
                targets = node.targets
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                value = node.value
                targets = [node.target]
            if value is not None and _is_container_expr(value):
                names: set[str] = set()
                for target in targets:
                    _target_names(target, names)
                for name in sorted(names):
                    if name in _SKIP_ASSIGN_NAMES:
                        continue
                    full = f"{prefix}.{name}" if prefix else name
                    out.append((rel, full, name, prefix))
                continue
            for _field, child in ast.iter_fields(node):
                if isinstance(child, list):
                    statements = [item for item in child if isinstance(item, ast.stmt)]
                    if statements:
                        scan(rel, statements, prefix)

    for rel, tree in trees.items():
        scan(rel, getattr(tree, "body", []), None)
    return out


def build_usage_kind(trees: dict[str, ast.AST]) -> tuple[dict, dict[str, int]]:
    """maps/keyreads.json.gz usage_kind: NAME is ``keyable`` or ``opaque``."""
    name_refs: dict[str, str] = {}
    attr_refs: dict[str, str] = {}
    class_refs: dict[tuple[str, str], str] = {}
    for tree in trees.values():
        _collect_refs(tree, name_refs, attr_refs, class_refs)

    table: dict[str, dict[str, str]] = {}
    stats = {"keyable": 0, "opaque": 0, "reasons": {}}
    reasons: dict[str, int] = {}
    for rel, full, bare, qualname in _container_definitions(trees):
        candidates = [attr_refs.get(bare)]
        candidates.append(
            name_refs.get(bare) if qualname is None else class_refs.get((qualname, bare))
        )
        used = any(reason is not None for reason in candidates)
        opaque = next((reason for reason in candidates if reason), None)
        if used and opaque is None:
            kind = "keyable"
        else:
            kind = "opaque"
            reason = opaque or "no-references"
            reasons[reason] = reasons.get(reason, 0) + 1
        table.setdefault(rel, {})[full] = kind
        stats[kind] += 1
    stats["reasons"] = dict(sorted(reasons.items(), key=lambda item: (-item[1], item[0])))
    return {rel: dict(sorted(names.items())) for rel, names in sorted(table.items())}, stats


def build_keyreads(
    dynamic: dict,
    test_ids: list[str],
    usage_kind: dict[str, dict[str, str]],
    name_origin: dict[str, str],
    module_paths: dict[str, str],
) -> tuple[dict, dict[str, int]]:
    """maps/keyreads.json.gz: (test, NAME, key) pairs + the safety table."""
    known = set(test_ids)
    stats = {"tests": 0, "pairs": 0, "names": 0, "unclassified": 0, "unplaced": 0}
    exposure: dict[str, list[list[str]]] = {}
    for bucket, by_name in (dynamic.get("keys") or {}).items():
        if bucket not in known:
            continue  # module-scope / unknown reads: not attributable to a test
        pairs: set[tuple[str, str]] = set()
        for name, keys in by_name.items():
            if not isinstance(name, str) or not name:
                continue
            for key in keys:
                pairs.add((name, str(key)))
        if pairs:
            exposure[bucket] = [list(pair) for pair in sorted(pairs)]
            stats["names"] += len({name for name, _ in pairs})
    stats["tests"] = len(exposure)
    stats["pairs"] = sum(len(pairs) for pairs in exposure.values())

    # Every name the plugin actually proxied must have a classification.  Names
    # created at runtime in ways the static assignment scan misses fall back to
    # an opaque classification (conservative).
    for name, origin in name_origin.items():
        module = origin.split(":", 1)[0] if ":" in origin else ""
        rel = module_paths.get(module)
        if not rel:
            stats["unplaced"] += 1
            continue
        entry = usage_kind.setdefault(rel, {})
        if name not in entry:
            entry[name] = "opaque"
            usage_kind[rel] = dict(sorted(entry.items()))
            stats["unclassified"] += 1
    return {"exposure": dict(sorted(exposure.items())), "usage_kind": usage_kind}, stats


# ------------------------------------------------------------------- testimports
def _module_table(sqlglot_files: list[str]) -> dict[str, str]:
    table: dict[str, str] = {}
    for rel in sqlglot_files:
        if not rel.endswith(".py"):
            continue
        if rel.endswith("/__init__.py"):
            dotted = rel[: -len("/__init__.py")].replace("/", ".")
        elif rel == "sqlglot/__init__.py":
            dotted = "sqlglot"
        else:
            dotted = rel[:-3].replace("/", ".")
        table[dotted] = rel
    return table


def _module_level_imports(body):
    """Imports at module scope (descending control flow, not functions/classes)."""
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        else:
            for _field, child in ast.iter_fields(node):
                if isinstance(child, list):
                    statements = [item for item in child if isinstance(item, ast.stmt)]
                    if statements:
                        yield from _module_level_imports(statements)


def _absolute_base(current: str, is_package: bool, node: ast.ImportFrom) -> str | None:
    if not node.level:
        return node.module
    parts = current.split(".")
    if not is_package:
        parts = parts[:-1]
    if node.level > 1:
        parts = parts[: -(node.level - 1)] if node.level - 1 < len(parts) else []
    base = ".".join(part for part in parts if part)
    if node.module:
        base = f"{base}.{node.module}" if base else node.module
    return base


def _resolve_imports(
    tree: ast.AST, current: str, is_package: bool, table: dict[str, str]
) -> set[str]:
    found: set[str] = set()
    for node in _module_level_imports(getattr(tree, "body", [])):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in table:
                    found.add(alias.name)
            continue
        base = _absolute_base(current, is_package, node)
        if base and base in table:
            found.add(base)
        for alias in node.names:
            if alias.name == "*":
                continue
            candidate = f"{base}.{alias.name}" if base else alias.name
            if candidate in table:
                found.add(candidate)
    return found


def build_testimports(
    checkout: str,
    test_files: list[str],
    trees: dict[str, ast.AST],
    sqlglot_files: list[str],
) -> tuple[dict, dict[str, int]]:
    """maps/testimports.json.gz: test file -> transitive sqlglot modules."""
    table = _module_table(sqlglot_files)
    stats = {"test_files": 0, "modules": 0, "parse_errors": 0}

    def closure(tree: ast.AST, current: str, is_package: bool) -> set[str]:
        reached: set[str] = set()
        pending = list(_resolve_imports(tree, current, is_package, table))
        while pending:
            module = pending.pop()
            if module in reached:
                continue
            reached.add(module)
            rel = table.get(module)
            if rel is None:
                continue
            dep_tree = trees.get(rel)
            if dep_tree is None:
                continue
            pending.extend(_resolve_imports(dep_tree, module, rel.endswith("__init__.py"), table))
        return reached

    payload: dict[str, list[str]] = {}
    for test_file in sorted(test_files):
        try:
            tree = ast.parse(_read_text(os.path.join(checkout, test_file)))
        except Exception as error:  # noqa: BLE001
            log(f"WARNING: cannot parse {test_file}: {error}")
            stats["parse_errors"] += 1
            continue
        modules = sorted(closure(tree, test_file[:-3].replace("/", "."), False))
        payload[test_file] = modules
        stats["test_files"] += 1
        stats["modules"] += len(modules)
    return payload, stats


# ------------------------------------------------------------------------ main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", default="/work/sqlglot")
    parser.add_argument("--coverage", default=None, help="coverage.py data file")
    parser.add_argument("--trace", default="/work/trace_reads.json")
    parser.add_argument("--dynamic", default="/work/dynamic.json")
    parser.add_argument("--collect", default="/work/collect.txt")
    parser.add_argument("--tests-json", default=None, help="optional tests.json to pin the id set")
    parser.add_argument("--out", required=True, help="output directory (maps/)")
    parser.add_argument("--pinned-sha", default=PINNED_SHA)
    args = parser.parse_args(argv)

    checkout = os.path.realpath(args.checkout)
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    coverage_file = args.coverage or os.path.join(checkout, ".coverage")

    test_ids = load_test_ids(args.collect, args.tests_json)
    if not test_ids:
        log("ERROR: no test ids collected; refusing to write an empty index")
        return 2
    ordinal_of = {test_id: ordinal for ordinal, test_id in enumerate(test_ids)}
    log(f"tests_index: {len(test_ids)} ids")

    _write_bytes(
        os.path.join(out, "tests_index.json"), _json_bytes({"test_ids": test_ids})
    )

    per_file, cov_stats = load_coverage_data(coverage_file, checkout, ordinal_of)
    log(
        "coverage: files={} pairs={} unknown_contexts={} outside_files={} import_time={}".format(
            len(per_file),
            sum(len(tests) for tests in per_file.values()),
            cov_stats["unknown_contexts"],
            cov_stats["outside_files"],
            cov_stats["empty"],
        )
    )
    if cov_stats["unknown_contexts"]:
        log("WARNING: coverage contexts not present in tests_index were ignored")
    coverage_size = write_coverage(os.path.join(out, "coverage.bin.gz"), per_file)

    sqlglot_files = _sqlglot_files(checkout)
    trees: dict[str, ast.AST] = {}
    sources: dict[str, str] = {}
    for rel in sqlglot_files:
        text = _read_text(os.path.join(checkout, rel))
        sources[rel] = text
        try:
            trees[rel] = ast.parse(text, filename=rel)
        except SyntaxError as error:
            log(f"WARNING: cannot parse {rel}: {error}")

    symbols = {rel: file_symbols(tree) for rel, tree in trees.items()}
    symbols_size = _write_gz(os.path.join(out, "symbols.json.gz"), _json_bytes(symbols))
    log(f"symbols: files={len(symbols)} entries={sum(len(v) for v in symbols.values())}")

    usage, crossfile = build_usage(trees)
    usage_size = _write_gz(os.path.join(out, "usage.json.gz"), _json_bytes(usage))
    refs_size = _write_gz(os.path.join(out, "usage_refs.json.gz"), _json_bytes(crossfile))
    log(
        f"usage: files={len(usage)} names={sum(len(v) for v in usage.values())} "
        f"crossfile_names={len(crossfile)}"
    )

    trace: dict[str, list[str]] = {}
    if os.path.isfile(args.trace):
        trace = json.loads(_read_text(args.trace))
    else:
        log(f"WARNING: trace file {args.trace} is missing; fixture map will be empty")
    fixtures, fx_stats = build_fixtures(trace, test_ids, checkout)
    fixtures_size = _write_gz(os.path.join(out, "fixtures.json.gz"), _json_bytes(fixtures))
    log(
        "fixtures: files={} shared={} module_bucket_reads={} unknown_reads={}".format(
            fx_stats["fixture_files"],
            len(fixtures.get("__shared__", [])),
            fx_stats["module_bucket_reads"],
            fx_stats["unknown_bucket_reads"],
        )
    )

    source_size = write_source_tarball(os.path.join(out, "source.tar.gz"), checkout, sqlglot_files)

    dynamic: dict = {}
    dynamic_stats = {"parents": 0, "children": 0}
    if os.path.isfile(args.dynamic):
        dynamic, dynamic_stats = load_dynamic(args.dynamic)
    else:
        log(f"WARNING: dynamic file {args.dynamic} is missing; new maps will be empty")
    if dynamic_stats["children"] or dynamic_stats["parents"] > 1:
        log(
            "dynamic merge: parents={} children={}".format(
                dynamic_stats["parents"], dynamic_stats["children"]
            )
        )
    test_files = sorted(fixtures.get("__test_files__", {}))

    dialects, dia_stats = build_dialects(
        dynamic, test_ids, fixtures.get("__test_files__", {}), trees, checkout
    )
    dialects_size = _write_gz(os.path.join(out, "dialects.json.gz"), _json_bytes(dialects))
    log(
        "dialects: tests={} exposures={} default_fallbacks={} file_scope={} class_scope={}".format(
            dia_stats["exposure_tests"],
            dia_stats["exposure_names"],
            dia_stats["default_fallbacks"],
            len(dialects["file_scope"]),
            len(dialects["class_scope"]),
        )
    )

    usage_kind, kind_stats = build_usage_kind(trees)
    module_paths = _module_table(sqlglot_files)
    keyreads, kr_stats = build_keyreads(
        dynamic, test_ids, usage_kind, dynamic.get("names") or {}, module_paths
    )
    keyreads_size = _write_gz(os.path.join(out, "keyreads.json.gz"), _json_bytes(keyreads))
    log(
        "keyreads: tests={} pairs={} names={} keyable={} opaque={} "
        "unclassified={} unplaced={}".format(
            kr_stats["tests"],
            kr_stats["pairs"],
            kr_stats["names"],
            kind_stats["keyable"],
            kind_stats["opaque"],
            kr_stats["unclassified"],
            kr_stats["unplaced"],
        )
    )

    testimports, ti_stats = build_testimports(checkout, test_files, trees, sqlglot_files)
    imports_size = _write_gz(os.path.join(out, "testimports.json.gz"), _json_bytes(testimports))
    log(
        "testimports: files={} modules={} parse_errors={}".format(
            ti_stats["test_files"], ti_stats["modules"], ti_stats["parse_errors"]
        )
    )

    file_sha1 = {
        rel: hashlib.sha1(sources[rel].encode("utf-8")).hexdigest() for rel in sqlglot_files
    }
    manifest = {
        "pinned_sha": args.pinned_sha,
        "spec": "MAPS_SPEC.md v1",
        "generator": {
            "script": "tools/mapgen/gen_maps.py",
            "host_git_head": os.environ.get("BR_GENERATOR_GIT", ""),
            "sqlglot_source": f"https://codeload.github.com/tobymao/sqlglot/tar.gz/{args.pinned_sha}",
            "python": sys.version.split()[0],
            "coverage": _coverage_version(),
        },
        "counts": {
            "tests": len(test_ids),
            "sqlglot_files": len(sqlglot_files),
            "coverage_file_pairs": sum(len(tests) for tests in per_file.values()),
            "fixtures": fx_stats["fixture_files"],
            "keyread_pairs": kr_stats["pairs"],
        },
        "keyreads": {
            "proxied_names": len(dynamic.get("names") or {}),
            "skipped": list(dynamic.get("skipped") or []),
            "keyable": kind_stats["keyable"],
            "opaque": kind_stats["opaque"],
            "unclassified": kr_stats["unclassified"],
            "unplaced": kr_stats["unplaced"],
            "opaque_reasons": kind_stats["reasons"],
            "dynamic_stats": dynamic.get("stats") or {},
        },
        "capture": {
            "version": 2,
            "subprocess_trace": True,
            "dynamic_parents": dynamic_stats["parents"],
            "dynamic_children": dynamic_stats["children"],
        },
        "files_sha1": file_sha1,
    }
    manifest_size = len(_json_bytes(manifest))
    _write_bytes(os.path.join(out, "MANIFEST.json"), _json_bytes(manifest))

    total = sum(
        os.path.getsize(os.path.join(out, name))
        for name in (
            "tests_index.json",
            "coverage.bin.gz",
            "symbols.json.gz",
            "usage.json.gz",
            "usage_refs.json.gz",
            "fixtures.json.gz",
            "dialects.json.gz",
            "keyreads.json.gz",
            "testimports.json.gz",
            "source.tar.gz",
            "MANIFEST.json",
        )
    )
    log(
        "sizes: coverage={}B symbols={}B usage={}B refs={}B fixtures={}B dialects={}B "
        "keyreads={}B testimports={}B source={}B manifest={}B total={}B".format(
            coverage_size,
            symbols_size,
            usage_size,
            refs_size,
            fixtures_size,
            dialects_size,
            keyreads_size,
            imports_size,
            source_size,
            manifest_size,
            total,
        )
    )
    if total > 8 * 1024 * 1024:
        log("WARNING: maps/ exceeds the 8 MB budget")
    return 0


if __name__ == "__main__":
    sys.exit(main())
