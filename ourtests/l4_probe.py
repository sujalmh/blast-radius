"""Diagnostic: what does L4 do on the real maps? (stand-in class maps)

    python3 -m ourtests.l4_probe

``maps/`` ships without ``classes.json.gz`` / ``usage_style.json.gz`` until W1e
lands them, so this probe fabricates stand-in maps **in a temp directory**
(``maps/`` is never written) and reports the selection for the class-body
sample commits:

  * ``touched``   -- per test, the classes whose symbol span the test covers
                     (a static stand-in for "the test constructs the class"),
  * ``hierarchy`` -- static base classes from the pinned tarball,
  * ``usage_style`` -- reference lines of the probed names, split into
                     ``self``/``cls`` reads and everything else.

It is a measurement of the rule's shape, not a map generator: the real traces
are strictly better (a test can construct a class without covering its span).
"""

from __future__ import annotations

import ast
import gzip
import json
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blastradius import patch as patch_parsing, select as selection  # noqa: E402
from blastradius.maps_io import Maps, _class_module_names  # noqa: E402

COMMITS = ("ktz3", "4d63", "535z", "6ze8")
NAMES = ("arg_types", "NUMERIC_ESCAPES", "WEEK_OFFSET", "FUNCTIONS")
SELF_NAMES = frozenset({"self", "cls"})


def _sources(maps_dir: Path) -> dict[str, str]:
    texts: dict[str, str] = {}
    with tarfile.open(maps_dir / "source.tar.gz") as archive:
        for member in archive.getmembers():
            if not member.isfile() or not member.name.endswith(".py"):
                continue
            handle = archive.extractfile(member)
            if handle is not None:
                texts[member.name] = handle.read().decode("utf-8", "replace")
    return texts


def _hierarchy(texts: dict[str, str], symbols: dict) -> dict[str, list[str]]:
    class_names: dict[str, str] = {}
    for file, entries in symbols.items():
        modules = _class_module_names(file)
        if not modules:
            continue
        for qualname, _start, _end, kind in entries:
            if kind == "class":
                class_names.setdefault(qualname, f"{modules[0]}.{qualname}")
    edges: dict[str, list[str]] = {}
    for file, text in texts.items():
        modules = _class_module_names(file)
        if not modules:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        stack = [(tree, "")]
        while stack:
            node, prefix = stack.pop()
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    qualname = f"{prefix}{child.name}"
                    key = f"{modules[0]}.{qualname}"
                    bases = []
                    for base in child.bases:
                        if isinstance(base, ast.Name):
                            bases.append(class_names.get(base.id, base.id))
                        elif isinstance(base, ast.Attribute):
                            bases.append(class_names.get(base.attr, base.attr))
                    edges.setdefault(key, [])
                    edges[key].extend(b for b in bases if b != key)
                    stack.append((child, qualname + "."))
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    stack.append((child, prefix))
    return edges


def _usage_style(texts: dict[str, str], names: tuple[str, ...]) -> dict:
    style: dict[str, dict[str, dict[str, list[int]]]] = {name: {} for name in names}
    for file, text in texts.items():
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or node.attr not in style:
                continue
            entry = style[node.attr].setdefault(
                file, {"self_lines": [], "other_lines": []}
            )
            if isinstance(node.value, ast.Name) and node.value.id in SELF_NAMES:
                entry["self_lines"].append(node.lineno)
            else:
                entry["other_lines"].append(node.lineno)
            if isinstance(node.value, ast.Name) and node.value.id in style:
                style[node.value.id].setdefault(
                    file, {"self_lines": [], "other_lines": []
                })["other_lines"].append(node.lineno)
    for name, files in style.items():
        for entry in files.values():
            entry["self_lines"] = sorted(set(entry["self_lines"]))
            entry["other_lines"] = sorted(set(entry["other_lines"]))
    return style


def _touched(maps: Maps, symbols: dict, ids: list[str]) -> dict[str, list[str]]:
    touched: dict[str, list[str]] = {}
    for file, entries in symbols.items():
        modules = _class_module_names(file)
        if not modules:
            continue
        for qualname, start, end, kind in entries:
            if kind != "class":
                continue
            spelling = f"{modules[0]}.{qualname}"
            for ordinal in maps.covering_tests(file, [(start, end)]):
                if 0 <= ordinal < len(ids):
                    touched.setdefault(ids[ordinal], []).append(spelling)
    return touched


def main() -> int:
    maps_dir = ROOT / "maps"
    temp = Path(tempfile.mkdtemp(prefix="blastradius-l4-"))
    staged = temp / "maps"
    shutil.copytree(maps_dir, staged)
    data = temp / "data"
    (data / "commits").mkdir(parents=True)
    shutil.copy(ROOT / "sample" / "tests.json", data / "tests.json")
    for commit in COMMITS:
        shutil.copy(
            ROOT / "sample" / "commits" / f"{commit}.patch",
            data / "commits" / f"{commit}.patch",
        )

    maps = Maps.load(staged)
    ids = list(maps.test_ids())
    try:
        texts = _sources(staged)
        symbols = json.loads(gzip.decompress((staged / "symbols.json.gz").read_bytes()))
        touched = _touched(maps, symbols, ids)
        document = {
            "touched": touched,
            "hierarchy": _hierarchy(texts, symbols),
        }
        (staged / "classes.json.gz").write_bytes(
            gzip.compress(json.dumps(document).encode("utf-8"), mtime=0)
        )
        (staged / "usage_style.json.gz").write_bytes(
            gzip.compress(json.dumps(_usage_style(texts, NAMES)).encode("utf-8"), mtime=0)
        )
    finally:
        maps.close()

    for label, knobs in (("L4 on", None), ("L4 off", selection.Knobs(l4="off"))):
        fresh = Maps.load(staged)
        index = selection.TestIndex(fresh.test_ids(), {t: i for i, t in enumerate(ids)})
        try:
            print(f"== {label} (touched covers {len(document['touched'])}/{len(ids)} tests)")
            for warning in fresh.warnings:
                print(f"   warning: {warning}")
            for commit in COMMITS:
                text = (data / "commits" / f"{commit}.patch").read_text()
                chosen = selection.select_commit(
                    patch_parsing.parse_patch(text), fresh, index, knobs
                )
                print(
                    f"   {commit}: {len(chosen.tests):5d} tests  "
                    + ", ".join(str(hit) for hit in chosen.rules)
                )
        finally:
            fresh.close()
    shutil.rmtree(temp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
