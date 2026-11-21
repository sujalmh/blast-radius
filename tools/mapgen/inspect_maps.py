#!/usr/bin/env python3
"""Dump/inspect/validate a generated maps/ bundle (plumbing-run proof helper).

Examples:
  inspect_maps.py --maps maps
  inspect_maps.py --maps /out --file sqlglot/dialects/dune.py \
      --test tests/dialects/test_dune.py::TestDune::test_dune

Exits non-zero when an integrity check fails (gzip/JSON readable, ids in the
test index, paths in source.tar.gz, usage_kind names observed by the trace,
manifests consistent).  Everything is printed to stdout.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import tarfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gen_maps import _lines_to_runs, parse_coverage_payload  # noqa: E402

KINDS = {"keyable", "opaque"}


def read_gz_json(path: str):
    with gzip.open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--maps", default="maps")
    parser.add_argument("--file", default=None)
    parser.add_argument("--test", default=None)
    parser.add_argument("--limit", type=int, default=6)
    args = parser.parse_args()

    errors: list[str] = []

    index = read_json(os.path.join(args.maps, "tests_index.json"))["test_ids"]
    index_set = set(index)
    coverage = parse_coverage_payload(
        gzip.open(os.path.join(args.maps, "coverage.bin.gz"), "rb").read()
    )
    symbols = read_gz_json(os.path.join(args.maps, "symbols.json.gz"))
    fixtures = read_gz_json(os.path.join(args.maps, "fixtures.json.gz"))
    manifest = read_json(os.path.join(args.maps, "MANIFEST.json"))

    print(f"pinned_sha: {manifest['pinned_sha']}")
    print(f"tests_index: {len(index)} ids")
    print(f"  head: {index[:2]}")
    print(f"  tail: {index[-2:]}")
    print(f"coverage files: {len(coverage)}  symbols files: {len(symbols)}")

    # ------------------------------------------------------------ source members
    members: set[str] = set()
    try:
        with tarfile.open(os.path.join(args.maps, "source.tar.gz"), "r:gz") as tar:
            members = {member.name for member in tar.getmembers() if member.isfile()}
        dune = [name for name in sorted(members) if name.endswith("dialects/dune.py")]
        print(f"source.tar.gz: {len(members)} members (e.g. {dune})")
    except FileNotFoundError:
        errors.append("source.tar.gz: missing")

    # ---------------------------------------------------------------- new maps
    dialects = keyreads = testimports = None
    for name, target in (
        ("dialects.json.gz", "dialects"),
        ("keyreads.json.gz", "keyreads"),
        ("testimports.json.gz", "testimports"),
    ):
        path = os.path.join(args.maps, name)
        if not os.path.isfile(path):
            errors.append(f"{name}: missing")
            continue
        try:
            payload = read_gz_json(path)
        except Exception as error:  # noqa: BLE001
            errors.append(f"{name}: unreadable ({error})")
            continue
        if name.startswith("dialects"):
            dialects = payload
        elif name.startswith("keyreads"):
            keyreads = payload
        else:
            testimports = payload

    if dialects is not None:
        exposure = dialects.get("exposure", {})
        file_scope = dialects.get("file_scope", {})
        class_scope = dialects.get("class_scope", {})
        print(
            f"\ndialects.json.gz: exposure_tests={len(exposure)} "
            f"exposure_names={sum(len(v) for v in exposure.values())} "
            f"file_scope={len(file_scope)} class_scope={len(class_scope)}"
        )
        for test in (args.test,) if args.test else ():
            print(f"  {test}: {exposure.get(test)}")
        if args.test is None:
            for test in list(sorted(exposure))[: args.limit]:
                print(f"  {test}: {exposure[test]}")
        bad = sorted(set(exposure) - index_set)
        if bad:
            errors.append(f"dialects.json.gz: {len(bad)} exposure ids not in index (e.g. {bad[:2]})")
        for test, names in exposure.items():
            if not isinstance(names, list) or not names or not all(
                isinstance(name, str) and name for name in names
            ):
                errors.append(f"dialects.json.gz: bad exposure for {test}")
                break
        for path, scope in file_scope.items():
            if not isinstance(scope, str) or not scope:
                errors.append(f"dialects.json.gz: bad file_scope value for {path}")
            if members and path not in members:
                errors.append(f"dialects.json.gz: file_scope path missing from source: {path}")
        for path, scopes in class_scope.items():
            if members and path not in members:
                errors.append(f"dialects.json.gz: class_scope path missing from source: {path}")
            for klass, scope in scopes.items():
                if not isinstance(klass, str) or not isinstance(scope, str) or not scope:
                    errors.append(f"dialects.json.gz: bad class_scope entry in {path}")
        sample = ", ".join(f"{path}->{scope}" for path, scope in list(file_scope.items())[:4])
        print(f"  file_scope sample: {sample}")

    if keyreads is not None:
        exposure = keyreads.get("exposure", {})
        usage = keyreads.get("usage_kind", {})
        kinds = {}
        for names in usage.values():
            for kind in names.values():
                kinds[kind] = kinds.get(kind, 0) + 1
        print(
            f"\nkeyreads.json.gz: exposure_tests={len(exposure)} "
            f"pairs={sum(len(v) for v in exposure.values())} usage_files={len(usage)} "
            f"kinds={dict(sorted(kinds.items()))}"
        )
        bad = sorted(set(exposure) - index_set)
        if bad:
            errors.append(f"keyreads.json.gz: {len(bad)} exposure ids not in index (e.g. {bad[:2]})")
        known_names = {name for names in usage.values() for name in names}
        missing = 0
        for test in (args.test,) if args.test else list(sorted(exposure))[: args.limit]:
            pairs = exposure.get(test) or []
            print(f"  {test}: {len(pairs)} pairs")
            for pair in pairs[: args.limit]:
                print(f"    {pair[0]} -> {pair[1]!r}")
                if pair[0] not in known_names:
                    missing += 1
        for test, pairs in exposure.items():
            for pair in pairs:
                if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, str) for x in pair)):
                    errors.append(f"keyreads.json.gz: bad pair for {test}: {pair!r}")
                    break
        for path, names in usage.items():
            if members and path not in members:
                errors.append(f"keyreads.json.gz: usage_kind path missing from source: {path}")
            for name, kind in names.items():
                if kind not in KINDS:
                    errors.append(f"keyreads.json.gz: bad kind {kind!r} for {path}:{name}")
                    break
        if missing:
            errors.append(f"keyreads.json.gz: {missing} observed names have no usage_kind entry")

    if testimports is not None:
        test_files = fixtures.get("__test_files__", {})
        print(f"\ntestimports.json.gz: files={len(testimports)}")
        sample_keys = [args.test.split("::")[0]] if args.test else sorted(testimports)[: args.limit]
        for test_file in sample_keys:
            modules = testimports.get(test_file)
            print(f"  {test_file}: {len(modules or [])} modules")
            for module in (modules or [])[: args.limit]:
                print(f"    {module}")
        for test_file, modules in testimports.items():
            if test_files and test_file not in test_files:
                errors.append(f"testimports.json.gz: {test_file} is not a test file")
            if not isinstance(modules, list) or not modules:
                errors.append(f"testimports.json.gz: empty module list for {test_file}")
                continue
            for module in modules:
                if not isinstance(module, str) or not module.startswith("sqlglot"):
                    errors.append(f"testimports.json.gz: bad module {module!r} for {test_file}")
                    break
                rel = module.replace(".", "/")
                if members and not (rel + ".py" in members or rel + "/__init__.py" in members):
                    errors.append(f"testimports.json.gz: unknown module {module} for {test_file}")
                    break

    if args.file:
        path = args.file
        intervals = coverage.get(path, [])
        by_ordinal: dict[int, list[tuple[int, int]]] = {}
        for start, end, ordinal in intervals:
            by_ordinal.setdefault(ordinal, []).append((start, end))
        print(f"\ncoverage[{path}]: {len(intervals)} intervals from {len(by_ordinal)} tests")
        for ordinal in sorted(by_ordinal)[: args.limit]:
            lines = sorted(
                {line for start, end in by_ordinal[ordinal] for line in range(start, end + 1)}
            )
            print(f"  ordinal {ordinal} {index[ordinal] if ordinal < len(index) else '?'}")
            print(f"    lines {len(lines)} first={lines[:12]}{' ...' if len(lines) > 12 else ''}")

        entries = symbols.get(path, [])
        print(f"\nsymbols[{path}]: {len(entries)} entries")
        for entry in entries[: args.limit * 6]:
            print(f"  {entry}")

        target_ordinals = set(by_ordinal)
        if args.test:
            target_ordinals = {
                ordinal for ordinal, test_id in enumerate(index) if test_id == args.test
            }
            if not target_ordinals:
                print(f"  (test not found in index: {args.test})")
        overlap_found = False
        print(f"\noverlap check ({len(target_ordinals)} target test(s) vs symbols):")
        for qualname, start, end, kind in entries:
            hits = 0
            covered_lines = 0
            for ordinal in target_ordinals:
                for run_start, run_len in _lines_to_runs(
                    [
                        line
                        for s, e in by_ordinal.get(ordinal, [])
                        for line in range(s, e + 1)
                        if start <= line <= end
                    ]
                ):
                    covered_lines += run_len
                    hits += 1
            if covered_lines:
                overlap_found = True
                print(
                    f"  {kind} {qualname} [{start}-{end}]: covered_lines={covered_lines} "
                    f"runs={hits}"
                )
        print(f"  any_overlap={overlap_found}")

    if args.test:
        hits = sorted(path for path, tests in fixtures.items() if args.test in tests)
        print(f"\nfixtures read by {args.test}: {len(hits)}")
        for path in hits[: args.limit * 3]:
            print(f"  {path}")

    test_files = fixtures.get("__test_files__", {})
    print(f"\n__test_files__: {len(test_files)} files; __shared__: {fixtures.get('__shared__', [])}")

    print(f"\nintegrity: {len(errors)} error(s)")
    for error in errors:
        print(f"  ERROR {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
