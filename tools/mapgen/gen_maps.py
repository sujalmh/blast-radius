#!/usr/bin/env python3
"""Build the MAPS_SPEC.md map bundle from a pinned sqlglot checkout.

Inputs are produced by ``run_maps.sh``:

  * ``--collect``  ``pytest --collect-only -q`` output (test ids + order)
  * ``--coverage`` coverage.py sqlite data file, one context per test node id
  * ``--trace``    JSON from ``trace_conftest.py`` (test/module -> files read)

Outputs (all 0644, deterministic gzip: mtime=0, no filename in the header):

  tests_index.json, coverage.bin.gz, symbols.json.gz, usage.json.gz,
  fixtures.json.gz, source.tar.gz, MANIFEST.json

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


def assigned_names(tree: ast.AST) -> set[str]:
    """Names bound by Assign/AnnAssign at module or class body level."""
    names: set[str] = set()

    def scan(body) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue  # locals are not module/class-level names
            if isinstance(node, ast.ClassDef):
                scan(node.body)
                continue
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    _target_names(target, names)
                continue
            if isinstance(node, ast.AnnAssign):
                _target_names(node.target, names)
                continue
            # module/class level control flow: if/try/with/for bodies still bind
            # at the enclosing scope.
            for _field, value in ast.iter_fields(node):
                if isinstance(value, list):
                    statements = [item for item in value if isinstance(item, ast.stmt)]
                    if statements:
                        scan(statements)

    scan(getattr(tree, "body", []))
    return {name for name in names if name not in _SKIP_ASSIGN_NAMES}


def build_usage(trees: dict[str, ast.AST]):
    """Return (usage, crossfile).

    ``usage``     {file: {NAME: [lines in *that file* referencing NAME]}}
    ``crossfile`` {NAME: {file: [lines referencing NAME]}} for names whose
                  references reach a file other than where they are assigned.

    MAPS_SPEC v1 keys usage by the defining file and stores bare line numbers,
    which can only be sound for references in the same file.  ``crossfile`` is
    the unambiguous superset for selectors that want cross-module references;
    it is written to ``usage_refs.json.gz``.
    """
    references: dict[str, dict[str, set[int]]] = {}
    for rel, tree in trees.items():
        local: dict[str, set[int]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                local.setdefault(node.id, set()).add(node.lineno)
            elif isinstance(node, ast.Attribute):
                local.setdefault(node.attr, set()).add(node.lineno)
        for name, lines in local.items():
            references.setdefault(name, {})[rel] = lines

    usage: dict[str, dict[str, list[int]]] = {}
    defining: dict[str, set[str]] = {}
    for rel, tree in trees.items():
        names = assigned_names(tree)
        if not names:
            continue
        entry: dict[str, list[int]] = {}
        for name in sorted(names):
            defining.setdefault(name, set()).add(rel)
            entry[name] = sorted(references.get(name, {}).get(rel, ()))
        usage[rel] = entry

    crossfile: dict[str, dict[str, list[int]]] = {}
    for name in sorted(defining):
        by_file = references.get(name, {})
        files = defining[name]
        if any(path not in files for path in by_file):
            crossfile[name] = {path: sorted(lines) for path, lines in sorted(by_file.items())}
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


# ------------------------------------------------------------------------ main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", default="/work/sqlglot")
    parser.add_argument("--coverage", default=None, help="coverage.py data file")
    parser.add_argument("--trace", default="/work/trace_reads.json")
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
            "source.tar.gz",
            "MANIFEST.json",
        )
    )
    log(
        "sizes: coverage={}B symbols={}B usage={}B refs={}B fixtures={}B source={}B "
        "manifest={}B total={}B".format(
            coverage_size,
            symbols_size,
            usage_size,
            refs_size,
            fixtures_size,
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
