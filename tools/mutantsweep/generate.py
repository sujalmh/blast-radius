#!/usr/bin/env python3
"""Generate event-style mutants against the pinned sqlglot checkout (W5).

The sample commits from the event are single-token semantic flips.  This
generator reproduces that style with AST-driven operators: each mutant names a
(file, line) site plus the operator that rewrites it, and the operator locates
the *syntax node* at that site with ``ast``/``tokenize`` before touching a
single token.  Nothing is looked up from a patch/sample answer map.

Usage:
    python3 generate.py --checkout /path/to/pinned/sqlglot [--out corpus] [--work DIR]

The checkout must be the pristine pinned tree (sha eb11aec4...).  Mutated
copies are written under ``--work`` and each corpus patch is produced by
``diff -u`` from the pristine tree, with ``a/`` and ``b/`` labels so
``patch -p1`` and tools/mutantsweep/apply_patch.py both apply it.

Outputs, relative to this directory by default:
    corpus/<id>.patch        unified diff against the pinned commit
    corpus/manifest.json     id / kind / file / summary / line for every mutant
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tokenize

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"

# --------------------------------------------------------------------------
# Mutant table.  ``op`` selects the AST operator; the rest are operator args.
# Every site below is a single-token semantic flip in the style of the sample
# commits (2jrq/8y7j/4d63/535z/ktz3/6ze8/2haz/...).
# --------------------------------------------------------------------------
MUTANTS = [
    # a. and <-> or inside multi-term conditions in method bodies
    {
        "id": "andor_simplify",
        "kind": "and_or",
        "group": "a",
        "file": "sqlglot/optimizer/simplify.py",
        "line": 206,
        "op": "boolop_flip",
        "from": "and",
        "summary": "propagate_constants guard: `and (root or ...)` -> `or (root or ...)`",
    },
    {
        "id": "andor_bq_parser",
        "kind": "and_or",
        "group": "a",
        "file": "sqlglot/parsers/bigquery.py",
        "line": 476,
        "op": "boolop_flip",
        "from": "and",
        "summary": "BigQuery dotted-identifier fixup: `len(parts) == 2 and ...` -> `or`",
    },
    # b. == <-> != and is <-> is not in method bodies
    {
        "id": "eqne_oracle_gen",
        "kind": "eq_ne",
        "group": "b",
        "file": "sqlglot/generators/oracle.py",
        "line": 154,
        "op": "compare_flip",
        "to": "ne",
        "summary": "Oracle table versioning: `kind == \"AS OF\"` -> `!=`",
    },
    {
        "id": "isnone_tsql_gen",
        "kind": "is_none",
        "group": "b",
        "file": "sqlglot/generators/tsql.py",
        "line": 446,
        "op": "compare_flip",
        "to": "isnot",
        "summary": "TSQL time precision: `fractions is None` -> `is not None`",
    },
    # c. class-body literal nudges in dialect / generator / typing modules
    {
        "id": "classlit_hive_regexp_group",
        "kind": "class_literal",
        "group": "c",
        "file": "sqlglot/dialects/hive.py",
        "line": 30,
        "op": "class_int_delta",
        "delta": 1,
        "summary": "Hive class body: REGEXP_EXTRACT_DEFAULT_GROUP 1 -> 2",
    },
    {
        "id": "classlit_duckdb_sample",
        "kind": "class_literal",
        "group": "c",
        "file": "sqlglot/generators/duckdb.py",
        "line": 1612,
        "op": "class_str_swap",
        "char_index": 7,
        "summary": "DuckDB class body: TABLESAMPLE_KEYWORDS \"USING SAMPLE\" -> \"USING SMPLE\"",
    },
    {
        "id": "classlit_typing_null",
        "kind": "class_literal",
        "group": "c",
        "file": "sqlglot/typing/__init__.py",
        "line": 433,
        "op": "metadata_attr_swap",
        "old_attr": "NULL",
        "new_attr": "UNKNOWN",
        "summary": "typing metadata: exp.Null returns DType.NULL -> DType.UNKNOWN",
    },
    # d. arg_types / tokenizer / parser table entry flips in class bodies
    {
        "id": "argtypes_groupconcat",
        "kind": "arg_types",
        "group": "d",
        "file": "sqlglot/expressions/aggregate.py",
        "line": 143,
        "op": "arg_types_bool",
        "summary": "GroupConcat arg_types: \"separator\": False -> True",
    },
    {
        "id": "tokenizer_oracle_neq",
        "kind": "tokenizer_table",
        "group": "d",
        "file": "sqlglot/dialects/oracle.py",
        "line": 104,
        "op": "tokenizer_table",
        "key": "^=",
        "old_attr": "NEQ",
        "new_attr": "EQ",
        "summary": "Oracle Tokenizer: \"^=\" TokenType.NEQ -> TokenType.EQ",
    },
    {
        "id": "parsertbl_presto_datediff",
        "kind": "parser_table",
        "group": "d",
        "file": "sqlglot/parsers/presto.py",
        "line": 131,
        "op": "parser_table_argidx",
        "table_key": "DATE_DIFF",
        "kw": "this",
        "old_idx": 2,
        "new_idx": 0,
        "summary": "Presto DATE_DIFF builder: this=args[2] -> this=args[0]",
    },
    # e. optimizer module condition flips
    {
        "id": "optcond_optimize_joins",
        "kind": "optimizer_cond",
        "group": "e",
        "file": "sqlglot/optimizer/optimize_joins.py",
        "line": 49,
        "op": "compare_flip",
        "to": "gt",
        "summary": "optimize_joins conjunct extraction: `len(...) < 2` -> `> 2`",
    },
    {
        "id": "optcond_pushdown_pos",
        "kind": "optimizer_cond",
        "group": "e",
        "file": "sqlglot/optimizer/pushdown_predicates.py",
        "line": 94,
        "op": "compare_flip",
        "to": "le",
        "summary": "pushdown_predicates: `position >= last_null_extending` -> `<=`",
    },
    # f. fixture .sql single-line edits (2 optimizer fixtures + identity fixture)
    {
        "id": "fixture_eliminate_joins",
        "kind": "fixture",
        "group": "f",
        "file": "tests/fixtures/optimizer/eliminate_joins.sql",
        "line": 58,
        "op": "fixture_line",
        "old": "  LIMIT 1",
        "new": "  LIMIT 2",
        "summary": "eliminate_joins fixture: DISTINCT ON LIMIT 1 -> LIMIT 2 (input line)",
    },
    {
        "id": "fixture_qualify_columns",
        "kind": "fixture",
        "group": "f",
        "file": "tests/fixtures/optimizer/qualify_columns.sql",
        "line": 32,
        "op": "fixture_line",
        "old": "SELECT a AS b FROM x;",
        "new": "SELECT a AS c FROM x;",
        "summary": "qualify_columns fixture: output alias b -> c (input line)",
    },
    {
        "id": "fixture_identity_cast",
        "kind": "fixture",
        "group": "f",
        "file": "tests/fixtures/identity.sql",
        "line": 390,
        "op": "fixture_line",
        "old": "SELECT CAST(a AS INT) FROM test",
        "new": "SELECT CAST(a AS INT FROM test",
        "summary": "identity fixture: drop the closing paren in CAST (parse-error edit)",
    },
    # g. controls
    {
        "id": "control_comment_oracle",
        "kind": "comment",
        "group": "g",
        "file": "sqlglot/generators/oracle.py",
        "line": 15,
        "op": "comment_insert",
        "text": "# Kept for parity with the reference implementation",
        "summary": "comment-only insertion (known neutral control)",
    },
    {
        "id": "control_enum_dialect",
        "kind": "enum_is",
        "group": "g",
        "file": "sqlglot/dialects/dialect.py",
        "line": 1223,
        "op": "compare_flip",
        "to": "eq",
        "summary": "enum comparison: `strategy is NormalizationStrategy.X` -> `==` (neutral control)",
    },
]


# --------------------------------------------------------------------------
# Source-position helpers
# --------------------------------------------------------------------------
class Source:
    def __init__(self, text: str, parse: bool = True):
        self.text = text
        self.starts = [0]
        for line in text.splitlines(keepends=True):
            self.starts.append(self.starts[-1] + len(line))
        self.tree = None
        self.parents = {}
        self.tokens = []
        if not parse:
            return
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            self.tokens.append(tok)
        self.tree = ast.parse(text)
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node

    def abs(self, lineno: int, col: int) -> int:
        """(lineno, col) -> absolute character offset (AST cols are utf-8 bytes)."""
        start = self.starts[lineno - 1]
        line = self.text[start:].split("\n", 1)[0]
        if col == 0:
            return start
        return start + len(line.encode("utf-8")[:col].decode("utf-8", "ignore"))

    def start(self, node: ast.AST) -> int:
        return self.abs(node.lineno, node.col_offset)

    def end(self, node: ast.AST) -> int:
        # end_col_offset is exclusive
        return self.abs(node.end_lineno, node.end_col_offset)

    def tok_abs(self, tok) -> tuple[int, int]:
        return self.abs(tok.start[0], tok.start[1]), self.abs(tok.end[0], tok.end[1])

    def tokens_between(self, lo: int, hi: int):
        for tok in self.tokens:
            if tok.type in (tokenize.NAME, tokenize.OP, tokenize.NUMBER, tokenize.STRING):
                a, b = self.tok_abs(tok)
                if a >= lo and b <= hi:
                    yield tok, a, b

    def enclosing(self, node: ast.AST, types) -> ast.AST | None:
        cur = self.parents.get(node)
        while cur is not None:
            if isinstance(cur, types):
                return cur
            cur = self.parents.get(cur)
        return None

    def enclosing_class(self, node) -> ast.ClassDef | None:
        return self.enclosing(node, ast.ClassDef)

    def in_method(self, node) -> bool:
        return self.enclosing(node, (ast.FunctionDef, ast.AsyncFunctionDef)) is not None

    def line_text(self, lineno: int) -> str:
        return self.text.splitlines()[lineno - 1]


def _one(cands, what: str):
    if len(cands) != 1:
        raise SystemExit(f"generator: expected exactly 1 {what}, found {len(cands)}")
    return cands[0]


# --------------------------------------------------------------------------
# Operators: each returns a list of (start_abs, end_abs, replacement)
# --------------------------------------------------------------------------
def op_boolop_flip(src: Source, spec) -> list[tuple[int, int, str]]:
    line = spec["line"]
    cands = []
    for node in ast.walk(src.tree):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, (ast.And, ast.Or)):
            continue
        if not src.in_method(node):
            continue
        lo, hi = src.end(node.values[0]), src.start(node.values[1])
        for tok, a, b in src.tokens_between(lo, hi):
            if tok.type != tokenize.NAME or tok.string not in ("and", "or"):
                continue
            if tok.start[0] != line:
                continue
            if spec.get("from") and tok.string != spec["from"]:
                continue
            cands.append((tok, a, b))
    tok, a, b = _one(cands, f"{spec['file']}:{line} boolean operator")
    return [(a, b, "or" if tok.string == "and" else "and")]


_COMPARE_REWRITES = {
    "eq": {"is": (None, "=="), "==": (None, "==")},
    "ne": {"==": (None, "!="), "is": (None, "!=")},
    "isnot": {"is": (None, "is not")},
    "gt": {"<": (None, ">")},
    "le": {">=": (None, "<=")},
}


def op_compare_flip(src: Source, spec) -> list[tuple[int, int, str]]:
    line = spec["line"]
    to = spec["to"]
    cands = []
    for node in ast.walk(src.tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not src.in_method(node):
            continue
        lo, hi = src.end(node.left), src.start(node.comparators[0])
        toks = [
            (tok, a, b)
            for tok, a, b in src.tokens_between(lo, hi)
            if tok.type in (tokenize.NAME, tokenize.OP)
        ]
        text = " ".join(t.string for t, _, _ in toks)
        if line not in range(node.lineno, (node.end_lineno or node.lineno) + 1):
            continue
        cands.append((toks, text, node))
    if len(cands) > 1:
        # several comparisons share the line; keep only ones whose operator is on it
        cands = [c for c in cands if c[0] and c[0][0][0].start[0] == line]
    toks, text, node = _one(cands, f"{spec['file']}:{line} comparison")
    if text in ("is not",):
        key = "is not"
    elif text in ("is", "==", "!=", "<", ">="):
        key = text
    else:
        raise SystemExit(f"generator: unsupported comparison operator {text!r}")
    table = _COMPARE_REWRITES[to]
    if to == "eq" and key == "is":
        # is -> == : replace the `is` token
        tok, a, b = toks[0]
        return [(a, b, "==")]
    if to == "isnot" and key == "is":
        tok, a, b = toks[0]
        return [(a, b, "is not")]
    if key not in table:
        raise SystemExit(f"generator: cannot rewrite {key!r} to {to!r}")
    tok, a, b = toks[0]
    return [(a, b, table[key][1])]


def _class_assign_on_line(src: Source, spec):
    line = spec["line"]
    cands = []
    for node in ast.walk(src.tree):
        if not isinstance(node, ast.Assign):
            continue
        cls = src.enclosing_class(node)
        if cls is None:
            continue
        if not (node.lineno <= line <= (node.end_lineno or node.lineno)):
            continue
        cands.append((node, cls))
    return _one(cands, f"{spec['file']}:{line} class-body assignment")


def op_class_int_delta(src: Source, spec) -> list[tuple[int, int, str]]:
    node, cls = _class_assign_on_line(src, spec)
    value = node.value
    sign = 1
    if isinstance(value, ast.UnaryOp) and isinstance(value.op, ast.USub):
        sign = -1
        value = value.operand
    if not isinstance(value, ast.Constant) or not isinstance(value.value, int):
        raise SystemExit(f"generator: {cls.name} assignment on line {spec['line']} is not an int")
    new = value.value + sign * spec["delta"]
    tok = _one(
        [
            (t, a, b)
            for t, a, b in src.tokens_between(src.start(value), src.end(value))
            if t.type == tokenize.NUMBER
        ],
        "integer literal",
    )
    return [(tok[1], tok[2], str(new))]


def op_class_str_swap(src: Source, spec) -> list[tuple[int, int, str]]:
    node, _cls = _class_assign_on_line(src, spec)
    value = node.value
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        raise SystemExit(f"generator: assignment on line {spec['line']} is not a string")
    tok = _one(
        [
            (t, a, b)
            for t, a, b in src.tokens_between(src.start(value), src.end(value))
            if t.type == tokenize.STRING
        ],
        "string literal",
    )
    # token text includes quotes; drop one interior character (char swap style)
    raw = tok[0].string
    idx = spec["char_index"]
    mutated = raw[:idx] + raw[idx + 1 :]
    if mutated == raw or len(mutated) <= 2:
        raise SystemExit("generator: string swap produced no change")
    return [(tok[1], tok[2], mutated)]


def op_metadata_attr_swap(src: Source, spec) -> list[tuple[int, int, str]]:
    line = spec["line"]
    old_attr = spec["old_attr"]
    cands = []
    for node in ast.walk(src.tree):
        if not isinstance(node, ast.Attribute) or node.attr != old_attr:
            continue
        if node.lineno != line:
            continue
        # must live inside a metadata dict assigned at module or class scope
        cur, in_dict = src.parents.get(node), False
        while cur is not None:
            if isinstance(cur, ast.Dict):
                in_dict = True
                break
            cur = src.parents.get(cur)
        if not in_dict:
            continue
        cands.append(node)
    node = _one(cands, f"{spec['file']}:{line} attribute {old_attr}")
    start = src.abs(node.end_lineno, node.end_col_offset - len(old_attr.encode()))
    return [(start, src.end(node), spec["new_attr"])]


def op_arg_types_bool(src: Source, spec) -> list[tuple[int, int, str]]:
    line = spec["line"]
    cands = []
    for node in ast.walk(src.tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Dict):
                continue
            targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            if "arg_types" not in targets:
                continue
            for key, value in zip(stmt.value.keys, stmt.value.values):
                if key is None or key.lineno != line:
                    continue
                if isinstance(value, ast.Constant) and isinstance(value.value, bool):
                    cands.append((node, key, value))
    cls, key, value = _one(cands, f"{spec['file']}:{line} arg_types bool entry")
    tok = _one(
        [
            (t, a, b)
            for t, a, b in src.tokens_between(src.start(value), src.end(value))
            if t.type == tokenize.NAME and t.string in ("True", "False")
        ],
        "bool literal",
    )
    new = "False" if value.value else "True"
    return [(tok[1], tok[2], new)]


def op_tokenizer_table(src: Source, spec) -> list[tuple[int, int, str]]:
    line = spec["line"]
    old_attr, new_attr = spec["old_attr"], spec["new_attr"]
    cands = []
    for node in ast.walk(src.tree):
        if not isinstance(node, ast.ClassDef) or node.name != "Tokenizer":
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Attribute) or sub.attr != old_attr:
                continue
            if sub.lineno != line:
                continue
            in_dict = False
            cur = src.parents.get(sub)
            while cur is not None and cur is not node:
                if isinstance(cur, ast.Dict):
                    in_dict = True
                    break
                cur = src.parents.get(cur)
            if in_dict:
                cands.append((node, sub))
    _cls, node = _one(cands, f"{spec['file']}:{line} Tokenizer table {old_attr}")
    start = src.abs(node.end_lineno, node.end_col_offset - len(old_attr.encode()))
    return [(start, src.end(node), new_attr)]


def op_parser_table_argidx(src: Source, spec) -> list[tuple[int, int, str]]:
    line = spec["line"]
    key_name = spec["table_key"]
    kw, old_idx, new_idx = spec["kw"], spec["old_idx"], spec["new_idx"]
    cands = []
    for node in ast.walk(src.tree):
        if not isinstance(node, ast.ClassDef) or not node.name.endswith("Parser"):
            continue
        table = None
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
                if "FUNCTIONS" in targets and isinstance(stmt.value, ast.Dict):
                    table = stmt.value
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if stmt.target.id == "FUNCTIONS" and isinstance(stmt.value, ast.Dict):
                    table = stmt.value
        if table is None:
            continue
        for key, value in zip(table.keys, table.values):
            if not (isinstance(key, ast.Constant) and key.value == key_name):
                continue
            for call in ast.walk(value):
                if not isinstance(call, ast.Call):
                    continue
                fn = call.func
                fname = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                if fname != "seq_get" or len(call.args) < 2:
                    continue
                if call.lineno != line:
                    continue
                idx_node = call.args[1]
                if not (isinstance(idx_node, ast.Constant) and idx_node.value == old_idx):
                    continue
                targets = [k.arg for k in call.keywords]
                if kw not in targets:
                    cands.append((node, idx_node))
    _cls, idx_node = _one(cands, f"{spec['file']}:{line} {key_name} seq_get index")
    return [(src.start(idx_node), src.end(idx_node), str(new_idx))]


def op_comment_insert(src: Source, spec) -> list[tuple[int, int, str]]:
    line = spec["line"]
    text = src.line_text(line)
    pos = src.starts[line]  # offset of the start of the following line
    return [(pos, pos, f"{spec['text']}\n")]


def op_fixture_line(src: Source, spec) -> list[tuple[int, int, str]]:
    line = spec["line"]
    current = src.line_text(line)
    if current != spec["old"]:
        raise SystemExit(
            f"generator: {spec['file']}:{line} is {current!r}, expected {spec['old']!r}"
        )
    start = src.starts[line - 1]
    return [(start, start + len(current), spec["new"])]


OPS = {
    "boolop_flip": op_boolop_flip,
    "compare_flip": op_compare_flip,
    "class_int_delta": op_class_int_delta,
    "class_str_swap": op_class_str_swap,
    "metadata_attr_swap": op_metadata_attr_swap,
    "arg_types_bool": op_arg_types_bool,
    "tokenizer_table": op_tokenizer_table,
    "parser_table_argidx": op_parser_table_argidx,
    "comment_insert": op_comment_insert,
    "fixture_line": op_fixture_line,
}


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
def generate_one(spec, checkout: str, work: str, out: str) -> dict:
    rel = spec["file"]
    pristine_path = os.path.join(checkout, rel)
    with open(pristine_path, "r", encoding="utf-8") as fh:
        text = fh.read()

    src = Source(text, parse=rel.endswith(".py"))
    edits = OPS[spec["op"]](src, spec)
    edits.sort(key=lambda e: e[0], reverse=True)
    mutated = text
    for start, end, replacement in edits:
        mutated = mutated[:start] + replacement + mutated[end:]
    if mutated == text:
        raise SystemExit(f"generator: {spec['id']} produced no change")

    if rel.endswith(".py"):
        try:
            after = ast.parse(mutated)
        except SyntaxError as exc:
            raise SystemExit(f"generator: {spec['id']} produced invalid python: {exc}")
        if spec["op"] == "comment_insert" and ast.dump(after) != ast.dump(src.tree):
            raise SystemExit("generator: comment control changed the AST")

    mut_path = os.path.join(work, "mutated", spec["id"], rel)
    os.makedirs(os.path.dirname(mut_path), exist_ok=True)
    with open(mut_path, "w", encoding="utf-8") as fh:
        fh.write(mutated)

    patch_name = f"{spec['id']}.patch"
    patch_path = os.path.join(out, patch_name)
    proc = subprocess.run(
        [
            "diff",
            "-u",
            "--label",
            f"a/{rel}",
            "--label",
            f"b/{rel}",
            pristine_path,
            mut_path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1) or not proc.stdout.strip():
        raise SystemExit(f"generator: diff failed for {spec['id']}: {proc.stderr}")
    with open(patch_path, "w", encoding="utf-8") as fh:
        fh.write(proc.stdout)

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from apply_patch import apply_patch  # noqa: E402  (local sibling module)

    check = os.path.join(work, "check", spec["id"])
    shutil.rmtree(check, ignore_errors=True)
    os.makedirs(check)
    dst = os.path.join(check, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(pristine_path, dst)
    apply_patch(patch_path, check)
    with open(dst, "r", encoding="utf-8") as fh:
        if fh.read() != mutated:
            raise SystemExit(f"generator: {spec['id']} patch round-trip mismatch")

    return {
        "id": spec["id"],
        "kind": spec["kind"],
        "group": spec["group"],
        "file": rel,
        "line": spec["line"],
        "summary": spec["summary"],
        "patch": patch_name,
        "sha256": hashlib.sha256(mutated.encode()).hexdigest(),
        "diff_lines": proc.stdout.count("\n"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkout", required=True, help="pristine pinned sqlglot tree")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus"))
    ap.add_argument("--work", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".work"))
    args = ap.parse_args()

    for required in ("sqlglot/__init__.py", "tests/helpers.py", "pyproject.toml"):
        if not os.path.isfile(os.path.join(args.checkout, required)):
            raise SystemExit(f"not a pinned sqlglot checkout: {args.checkout} (missing {required})")

    os.makedirs(args.out, exist_ok=True)
    os.makedirs(args.work, exist_ok=True)
    manifest = []
    for spec in MUTANTS:
        entry = generate_one(spec, args.checkout, args.work, args.out)
        manifest.append(entry)
        print(
            f"[generate] {entry['id']:<28} {entry['kind']:<16} {entry['file']}:{entry['line']}",
            file=sys.stderr,
        )

    manifest_path = os.path.join(args.out, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump({"pin": PIN, "mutants": manifest}, fh, indent=2)
        fh.write("\n")
    print(f"[generate] wrote {len(manifest)} mutants to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
