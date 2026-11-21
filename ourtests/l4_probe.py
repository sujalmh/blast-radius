"""Diagnostic: what does L4 do on the real maps? (stand-in class maps)

    python3 -m ourtests.l4_probe

``maps/`` ships without ``classes.json.gz`` / ``usage_style.json.gz`` until W1f
lands them, so this probe fabricates stand-in maps **in a temp directory**
(``maps/`` is never written) and reports the selection for the class-body
sample commits:

  * ``touched``   -- per test, the classes whose symbol span the test covers
                     (a static stand-in for "the test constructs the class"),
  * ``hierarchy`` -- static base classes from the pinned tarball,
  * ``usage_style`` -- reference lines of the probed names split 3-ways by
                     receiver kind, exactly as W1f's map does:
                     ``self_lines`` (``self``/``cls``), ``class_lines`` (module
                     or class receiver, or a bare module-scope read),
                     ``instance_lines`` (plain variables/parameters).

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


def _static_receivers(tree: ast.AST) -> set[str]:
    """Names that statically resolve to a module or a class in this file.

    A receiver in this set is a ``class_lines`` read; anything else (a function
    parameter, a local) is an ``instance_lines`` read.
    """
    names: set[str] = set()

    def add_targets(node: ast.AST) -> None:
        targets = getattr(node, "targets", None)
        if targets is None:
            target = getattr(node, "target", None)
            targets = [target] if target is not None else []
        stack = list(targets)
        while stack:
            current = stack.pop()
            if isinstance(current, ast.Name):
                names.add(current.id)
            elif isinstance(current, (ast.Tuple, ast.List)):
                stack.extend(current.elts)
            elif isinstance(current, ast.Starred):
                stack.append(current.value)

    def visit(body) -> None:
        for statement in body:
            if isinstance(statement, (ast.Assign, ast.AugAssign)):
                add_targets(statement)
            elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
                add_targets(statement)
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(statement.name)  # a function object is a module binding
            elif isinstance(statement, ast.ClassDef):
                names.add(statement.name)
            elif isinstance(statement, ast.Import):
                for alias in statement.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(statement, ast.ImportFrom):
                for alias in statement.names:
                    if alias.name != "*":
                        names.add(alias.asname or alias.name)
            nested = getattr(statement, "body", None)
            if isinstance(nested, list):
                visit(nested)
            for handler in getattr(statement, "handlers", ()) or ():
                visit(handler.body)
            for field in ("orelse", "finalbody"):
                child = getattr(statement, field, None)
                if isinstance(child, list):
                    visit(child)

    visit(getattr(tree, "body", []))
    return names


def _usage_style(texts: dict[str, str], names: tuple[str, ...]) -> dict:
    """3-way receiver-split reference lines of ``names`` (W1f shape)."""
    style: dict[str, dict[str, dict[str, list[int]]]] = {
        name: {} for name in names
    }
    for file, text in texts.items():
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        static = _static_receivers(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in style:
                bucket_names = [node.attr]
                receiver = node.value
                if isinstance(receiver, ast.Name):
                    if receiver.id in SELF_NAMES:
                        bucket = "self_lines"
                    elif receiver.id in static:
                        bucket = "class_lines"
                    else:
                        bucket = "instance_lines"
                else:
                    bucket = "instance_lines"
                for name in bucket_names:
                    entry = style[name].setdefault(
                        file,
                        {"self_lines": [], "class_lines": [], "instance_lines": []},
                    )
                    entry[bucket].append(node.lineno)
            elif isinstance(node, ast.Name) and node.id in style:
                if not isinstance(node.ctx, ast.Load):
                    continue
                entry = style[node.id].setdefault(
                    file, {"self_lines": [], "class_lines": [], "instance_lines": []}
                )
                entry["class_lines"].append(node.lineno)
    for name, files in style.items():
        for entry in files.values():
            for bucket in ("self_lines", "class_lines", "instance_lines"):
                entry[bucket] = sorted(set(entry[bucket]))
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


def expression_hierarchy(hierarchy: dict, symbols: dict) -> dict:
    """Keep only the expression tree (what ``classes.json.gz`` actually traces).

    The real trace wraps ``Expr.__init__``, so dialect/parser/tokenizer classes
    are never in ``touched`` or ``hierarchy`` -- and L4 then keeps L1 for them.
    Restricting the stand-in to the same tree keeps the diagnostic faithful
    (the UNKNOWN-family behaviour is covered by the ``mockmaps`` unit tests).
    """
    keep: set[str] = set()
    for file, entries in symbols.items():
        if not file.startswith("sqlglot/expressions"):
            continue
        modules = _class_module_names(file)
        if not modules:
            continue
        for qualname, _start, _end, kind in entries:
            if kind == "class":
                keep.add(f"{modules[0]}.{qualname}")
    return {
        node: [base for base in bases if base in keep]
        for node, bases in hierarchy.items()
        if node in keep
    }


def expression_touched(maps: Maps, symbols: dict, ids: list[str]) -> dict:
    """Stand-in ``touched``: expression classes only (span coverers)."""
    touched = _touched(
        maps,
        {
            file: entries
            for file, entries in symbols.items()
            if file.startswith("sqlglot/expressions")
        },
        ids,
    )
    return touched


def main() -> int:
    maps_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "maps"
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
        touched = expression_touched(maps, symbols, ids)
        document = {
            "touched": touched,
            "hierarchy": expression_hierarchy(_hierarchy(texts, symbols), symbols),
        }
        (staged / "classes.json.gz").write_bytes(
            gzip.compress(json.dumps(document).encode("utf-8"), mtime=0)
        )
        (staged / "usage_style.json.gz").write_bytes(
            gzip.compress(
                json.dumps(_usage_style(texts, NAMES)).encode("utf-8"), mtime=0
            )
        )
    finally:
        maps.close()

    for label, knobs in (
        ("L4 on", None),
        ("L4 on, dialect narrow off", selection.Knobs(dialect="off")),
        ("L4 off", selection.Knobs(l4="off")),
    ):
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
