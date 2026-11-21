"""Enum-member comparison neutrality (``is`` <-> ``==``).

A hunk is *enum-neutral* when its only semantic change flips ``is``/``==`` (or
``is not``/``!=``) on a comparison whose two operands provably evaluate to
members of the same sqlglot ``Enum``.  Enum defines ``__eq__`` as identity, so
for two members of one enum the two spellings are interchangeable.

Everything is decided statically from the pinned sources:

* an *enum* is a class under ``sqlglot/`` whose (transitively resolved) bases
  reach ``enum.Enum`` and whose own body defines neither ``__eq__`` nor
  ``__ne__``;
* a *member* is an attribute chain such as ``NormalizationStrategy.UPPERCASE``
  that resolves through the file's import bindings to a member of such a class;
* the *other* operand is proven to be that same enum by (a) an annotation that
  resolves to the enum class, (b) every assignment / property-return site of
  its name constructing that enum (recursively, across the pinned sources), or
  (c) being a member of the same enum itself.

Deliberately **not** covered: ``x is None`` vs ``x == None``.  ``Expression``
overrides ``__eq__`` with structural equality, so that flip is a real semantic
change; leaving it selected is the correct conservatism.

The index is built lazily, and skipping it is nearly free: a hunk-level text
prefilter (:func:`maybe_enum_flip`) only lets a file through when one of its
``-``/``+`` line pairs is identical apart from an ``is``/``is not`` versus
``==``/``!=`` spelling, so a commit that never touches a comparison never
parses anything.  When a hunk does qualify, the index is built once per process
and every parse/index/assignment scan is cached on the instance for the rest of
the batch.
"""

from __future__ import annotations

import ast
import re
from typing import Callable, Iterable, Sequence

from . import neutral
from .patch import FilePatch

_ENUM_ROOTS = frozenset({"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"})
_FLIPS = frozenset({(ast.Is, ast.Eq), (ast.IsNot, ast.NotEq)})
_MAX_PROOF_DEPTH = 6

_CODING = re.compile(r"coding[:=]")
_IS_NOT_TOKEN = re.compile(r"\bis\s+not\b")
_IS_TOKEN = re.compile(r"\bis\b")
_WHITESPACE = re.compile(r"\s+")


def _folded(line: str) -> str:
    """``line`` with ``is not``/``is`` folded onto ``!=``/``==``, no whitespace.

    Only line *pairing* uses this: two lines that fold onto the same text can
    differ in nothing but the operator spelling.
    """
    folded = _IS_NOT_TOKEN.sub("!=", line)
    folded = _IS_TOKEN.sub("==", folded)
    return _WHITESPACE.sub("", folded)


def maybe_enum_flip(file_patch: FilePatch) -> bool:
    """Cheap textual gate: some ``-``/``+`` pair differs only by an op flip.

    The whole enum path (two source parses, then -- behind them -- the pinned
    enum index) is only worth paying for when a removed line and an added line
    of the *same* hunk are character-for-character identical once whitespace is
    dropped and ``is not``/``is`` are folded onto ``!=``/``==``.  That is the
    exact footprint of an operator-only edit, so the overwhelming majority of
    hunks (any commit that never touches a comparison) stop here and never
    build the index.

    The gate is deliberately one-sided: a flip spelled differently (operands
    re-wrapped or parenthesised across lines) pairs with nothing and the file
    is treated as an ordinary edit.  That costs a few extra selected tests, but
    it can never drop one.
    """
    for hunk in file_patch.hunks:
        added = hunk.added
        removed = hunk.removed
        if not added or not removed:
            continue
        # ``folded text -> the whitespace-free texts that fold onto it``.  Hunks
        # are small, so this keeps the scan linear in the hunk's size.
        pairs: dict[str, set[str]] = {}
        for line in added:
            pairs.setdefault(_folded(line), set()).add(_WHITESPACE.sub("", line))
        for line in removed:
            candidates = pairs.get(_folded(line))
            if not candidates:
                continue
            plain = _WHITESPACE.sub("", line)
            if any(other != plain for other in candidates):
                return True
    return False


def _module_of(path: str) -> str:
    without_suffix = path[: -len(".py")] if path.endswith(".py") else path
    parts = without_suffix.split("/")
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _base_name(node: ast.AST) -> str | None:
    """Last dotted component of a base-class expression (``a.b.C`` -> ``C``)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


class _ClassInfo:
    __slots__ = ("file", "module", "qualname", "node")

    def __init__(self, file: str, module: str, qualname: str, node: ast.ClassDef):
        self.file = file
        self.module = module
        self.qualname = qualname
        self.node = node

    @property
    def fqname(self) -> str:
        return f"{self.module}.{self.qualname}"


class EnumIndex:
    """Static enum/member/type resolver over the pinned sqlglot sources."""

    def __init__(
        self,
        source_files: Iterable[str],
        source_of: Callable[[str], str | None],
    ) -> None:
        self._files = tuple(source_files)
        self._source_of = source_of
        self._loaded = False
        self._trees: dict[str, ast.AST] = {}
        self._classes: dict[str, _ClassInfo] = {}
        self._enums: dict[str, frozenset[str]] = {}
        self._enum_eq: dict[str, bool] = {}
        self._imports: dict[str, dict[str, tuple[str, str]]] = {}
        self._annotations: dict[str, dict[str, list[ast.AST]]] = {}
        self._param_names: dict[str, frozenset[str]] = {}
        self._dynamic_names: dict[str, frozenset[str]] = {}
        self._dynamic_attrs: dict[str, frozenset[str]] = {}
        self._attr_sites: dict[str, list[tuple[str, ast.AST]]] | None = None
        self._name_sites: dict[str, dict[str, list[ast.AST]]] | None = None
        self._functions: dict[str, list[ast.AST]] = {}

    # -- small helpers ---------------------------------------------------

    def _module_classes(self, file: str) -> dict[str, _ClassInfo]:
        module = _module_of(file)
        return {
            info.qualname: info
            for info in self._classes.values()
            if info.module == module
        }

    def _class_info(self, fqname: str) -> _ClassInfo | None:
        return self._classes.get(fqname)

    # -- lazy index ------------------------------------------------------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        for file in self._files:
            text = self._source_of(file)
            if not text:
                continue
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                continue
            self._trees[file] = tree
            module = _module_of(file)
            self._collect_classes(file, module, tree.body, "")
            self._imports[file] = self._collect_imports(file, module, tree)
            self._annotations[file] = self._collect_annotations(tree)
            parameters, dynamic_names, dynamic_attrs = self._collect_bindings(tree)
            self._param_names[file] = frozenset(parameters)
            self._dynamic_names[file] = frozenset(dynamic_names)
            self._dynamic_attrs[file] = frozenset(dynamic_attrs)
            self._functions[file] = [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
        self._resolve_enums()

    def _collect_classes(
        self,
        file: str,
        module: str,
        body: Sequence[ast.stmt],
        prefix: str,
    ) -> None:
        for node in body:
            if not isinstance(node, ast.ClassDef):
                continue
            qualname = f"{prefix}{node.name}"
            self._classes[f"{module}.{qualname}"] = _ClassInfo(
                file, module, qualname, node
            )
            self._collect_classes(file, module, node.body, f"{qualname}.")

    def _collect_imports(
        self, file: str, module: str, tree: ast.AST
    ) -> dict[str, tuple[str, str]]:
        """``local name -> ("module"|"symbol", dotted target)`` for module scope."""
        bindings: dict[str, tuple[str, str]] = {}
        package = module.rpartition(".")[0]
        for node in getattr(tree, "body", ()):  # module-level imports only
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        bindings[alias.asname] = ("module", alias.name)
                    else:
                        head = alias.name.split(".", 1)[0]
                        bindings[head] = ("module", head)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                level = node.level or 0
                if level:
                    parts = package.split(".") if package else []
                    if level - 1 <= len(parts):
                        parts = parts[: len(parts) - (level - 1)] if level > 1 else parts
                    base = ".".join([part for part in parts if part] + ([base] if base else []))
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    target = f"{base}.{alias.name}" if base else alias.name
                    local = alias.asname or alias.name
                    # A class is preferred (imports of enum classes are the
                    # interesting case); resolution falls back to modules.
                    bindings[local] = ("symbol", target)
        return bindings

    def _collect_annotations(
        self, tree: ast.AST
    ) -> dict[str, list[tuple[ast.AST, int, int]]]:
        """Name -> ``(annotation, scope_start, scope_end)`` for lexical scopes.

        The scope is the enclosing function (or the module).  A class-body
        annotation is not a binding visible to unqualified names in methods, so
        class bodies are walked only for their nested functions.
        """
        annotations: dict[str, list[tuple[ast.AST, int, int]]] = {}
        module_end = getattr(tree, "end_lineno", None) or 10**9

        def add(name: str | None, annotation: ast.AST | None, scope: tuple[int, int]) -> None:
            if name and annotation is not None:
                annotations.setdefault(name, []).append((annotation, scope[0], scope[1]))

        def walk(node: ast.AST, scope: tuple[int, int]) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = (node.lineno, node.end_lineno or node.lineno)
                arguments = node.args
                for arg in (
                    list(arguments.posonlyargs)
                    + list(arguments.args)
                    + list(arguments.kwonlyargs)
                ):
                    add(arg.arg, arg.annotation, inner)
                if arguments.vararg is not None:
                    add(arguments.vararg.arg, arguments.vararg.annotation, inner)
                if arguments.kwarg is not None:
                    add(arguments.kwarg.arg, arguments.kwarg.annotation, inner)
                for statement in node.body:
                    walk(statement, inner)
                return
            if isinstance(node, ast.ClassDef):
                for statement in node.body:
                    walk(statement, scope)
                return
            if isinstance(node, ast.AnnAssign):
                target = node.target
                if isinstance(target, ast.Name):
                    add(target.id, node.annotation, scope)
                return
            for child in ast.iter_child_nodes(node):
                walk(child, scope)

        walk(tree, (1, module_end))
        return annotations

    def _collect_bindings(
        self, tree: ast.AST
    ) -> tuple[set[str], set[str], set[str]]:
        """Names whose value is not fully described by plain ``x = rhs`` sites.

        A function parameter's incoming value is not an assignment, and
        ``for``/``with``/walrus/``except`` targets bind without a value node we
        can resolve.  Treating those names as "all sites construct the enum"
        would be unsound, so they are excluded from the assignment proof.
        """
        parameters: set[str] = set()
        names: set[str] = set()
        attrs: set[str] = set()

        def collect(target: ast.AST | None) -> None:
            if target is None:
                return
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                attrs.add(target.attr)
            elif isinstance(target, (ast.Tuple, ast.List)):
                for element in target.elts:
                    collect(element)
            elif isinstance(target, ast.Starred):
                collect(target.value)

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                arguments = node.args
                for arg in (
                    list(arguments.posonlyargs)
                    + list(arguments.args)
                    + list(arguments.kwonlyargs)
                ):
                    parameters.add(arg.arg)
                if arguments.vararg is not None:
                    parameters.add(arguments.vararg.arg)
                if arguments.kwarg is not None:
                    parameters.add(arguments.kwarg.arg)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                collect(node.target)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    collect(item.optional_vars)
            elif isinstance(node, ast.comprehension):
                collect(node.target)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                names.add(node.name)
            elif isinstance(node, ast.NamedExpr):
                collect(node.target)
            elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
                names.add(node.name)
            elif isinstance(node, ast.MatchMapping) and node.rest:
                names.add(node.rest)
        return parameters, names, attrs

    @staticmethod
    def _target_names(target: ast.AST) -> tuple[set[str], set[str]]:
        """Names and attribute names bound by an assignment target."""
        names: set[str] = set()
        attrs: set[str] = set()

        def visit(node: ast.AST) -> None:
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                attrs.add(node.attr)
            elif isinstance(node, (ast.Tuple, ast.List)):
                for element in node.elts:
                    visit(element)
            elif isinstance(node, ast.Starred):
                visit(node.value)

        visit(target)
        return names, attrs

    def _resolve_enums(self) -> None:
        # Inheritance may point forward (a class defined before its enum base in
        # this walk), so iterate to a fixed point.
        resolved: dict[str, bool] = {}
        changed = True
        while changed:
            changed = False
            for fqname, info in self._classes.items():
                if fqname in resolved:
                    continue
                is_enum = False
                for base in info.node.bases:
                    name = _base_name(base)
                    if name is None:
                        continue
                    if name in _ENUM_ROOTS:
                        is_enum = True
                        break
                    target = self._resolve_class_expression(base, info.file)
                    if target is not None and resolved.get(target):
                        is_enum = True
                        break
                if is_enum:
                    resolved[fqname] = True
                    changed = True
        for fqname in resolved:
            info = self._classes[fqname]
            members: set[str] = set()
            has_eq = False
            for statement in info.node.body:
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if statement.name in ("__eq__", "__ne__"):
                        has_eq = True
                    continue
                targets: list[ast.AST] = []
                if isinstance(statement, ast.Assign):
                    targets = list(statement.targets)
                elif isinstance(statement, ast.AnnAssign):
                    targets = [statement.target]
                for target in targets:
                    if isinstance(target, ast.Name) and not target.id.startswith("_"):
                        members.add(target.id)
            self._enums[fqname] = frozenset(members)
            self._enum_eq[fqname] = has_eq

    # -- resolution ------------------------------------------------------

    def _resolve_class_expression(self, node: ast.AST, file: str) -> str | None:
        """Resolve a dotted expression to a known class fqname (or ``None``)."""
        resolved = self._resolve(node, file)
        if resolved is None:
            return None
        kind, target = resolved
        return target if kind == "class" else None

    def _resolve(self, node: ast.AST, file: str) -> tuple[str, str] | None:
        """``(kind, fqname)`` with kind in module/class/member/instance."""
        if isinstance(node, ast.Name):
            bindings = self._imports.get(file, {})
            binding = bindings.get(node.id)
            if binding is not None:
                kind, target = binding
                if kind == "module":
                    return "module", target
                # A symbol import: it may be a class, a module, or a value.
                if self._class_info(target) is not None:
                    return "class", target
                if any(name.startswith(target + ".") for name in self._classes):
                    return "module", target
                return None
            local = self._module_classes(file).get(node.id)
            if local is not None:
                return "class", local.fqname
            return None

        if isinstance(node, ast.Attribute):
            base = self._resolve(node.value, file)
            if base is None:
                return None
            kind, target = base
            if kind == "module":
                candidate = f"{target}.{node.attr}"
                if self._class_info(candidate) is not None:
                    return "class", candidate
                if any(name.startswith(candidate + ".") for name in self._classes):
                    return "module", candidate
                return None
            if kind == "class":
                candidate = f"{target}.{node.attr}"
                if self._class_info(candidate) is not None:
                    return "class", candidate
                if target in self._enums and node.attr in self._enums[target]:
                    return "member", target
                return None
            return None

        if isinstance(node, ast.Call):
            resolved = self._resolve(node.func, file)
            if resolved is None:
                return None
            kind, target = resolved
            if kind == "class" and target in self._enums:
                return "instance", target
            return None

        return None

    # -- proof -----------------------------------------------------------

    def _usable_enum(self, fqname: str) -> bool:
        return fqname in self._enums and not self._enum_eq.get(fqname, True)

    def _member_enum(self, node: ast.AST, file: str) -> str | None:
        resolved = self._resolve(node, file)
        if resolved is None or resolved[0] != "member":
            return None
        return resolved[1] if self._usable_enum(resolved[1]) else None

    @staticmethod
    def _operand_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    def _assignment_sites(self) -> None:
        if self._attr_sites is not None:
            return
        attr_sites: dict[str, list[tuple[str, ast.AST]]] = {}
        name_sites: dict[str, dict[str, list[ast.AST]]] = {}
        for file, tree in self._trees.items():
            per_file = name_sites.setdefault(file, {})

            # (1) Every assignment target, for *name* proofs: a local variable
            # must be assigned only enum values in every site of its file.
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    targets: list[ast.AST] = list(node.targets)
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    targets = [node.target]
                else:
                    continue
                for target in targets:
                    names, attrs = self._target_names(target)
                    for name in names:
                        per_file.setdefault(name, []).append(node.value)
                    for attr in attrs:
                        attr_sites.setdefault(attr, []).append((file, node.value))

            # (2) Attribute proofs additionally take Name targets assigned
            # directly in a *class body* (the class-attribute default) and
            # property returns.  A bare local variable that happens to share
            # the name is not evidence about `self.x`.
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not any(
                        _base_name(decorator) == "property"
                        for decorator in node.decorator_list
                    ):
                        continue
                    for inner in ast.walk(node):
                        if isinstance(inner, ast.Return) and inner.value is not None:
                            attr_sites.setdefault(node.name, []).append(
                                (file, inner.value)
                            )
            self._collect_class_body_assignments(file, tree.body, attr_sites)
        self._attr_sites = attr_sites
        self._name_sites = name_sites

    def _collect_class_body_assignments(
        self,
        file: str,
        body: Sequence[ast.stmt],
        attr_sites: dict[str, list[tuple[str, ast.AST]]],
    ) -> None:
        for statement in body:
            if isinstance(statement, ast.ClassDef):
                self._collect_class_body_assignments(file, statement.body, attr_sites)
                continue
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    names, _ = self._target_names(target)
                    for name in names:
                        attr_sites.setdefault(name, []).append(
                            (file, statement.value)
                        )
            elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
                names, _ = self._target_names(statement.target)
                for name in names:
                    attr_sites.setdefault(name, []).append(
                        (file, statement.value)
                    )

    def _annotation_enum(self, node: ast.Name, file: str) -> str | None:
        annotations = self._annotations.get(file, {}).get(node.id, [])
        in_scope = [
            annotation
            for annotation, start, end in annotations
            if start <= node.lineno <= end
        ]
        if not in_scope:
            return None
        resolved = {
            target
            for annotation in in_scope
            if (target := self._resolve_class_expression(annotation, file)) is not None
        }
        if len(resolved) == 1:
            return next(iter(resolved))
        return None

    def _proven_enum(
        self,
        node: ast.AST,
        file: str,
        target: str,
        depth: int = 0,
        seen: frozenset[tuple[str, str]] = frozenset(),
    ) -> bool:
        if depth > _MAX_PROOF_DEPTH or not self._usable_enum(target):
            return False
        resolved = self._resolve(node, file)
        if resolved is not None:
            kind, fqname = resolved
            if kind in ("member", "instance"):
                return fqname == target
            if kind == "class":
                return False  # the enum class itself is not a member value

        name = self._operand_name(node)
        if name is None:
            return False

        # (a) an annotation that resolves to the enum class.
        if isinstance(node, ast.Name):
            annotation = self._annotation_enum(node, file)
            if annotation == target:
                return True
            # A parameter's incoming value, a `for`/`with`/walrus/`except`
            # binding, or a tuple target is not described by `x = rhs` sites:
            # the assignment proof below would be unsound for it.
            if name in self._param_names.get(file, ()) or name in self._dynamic_names.get(
                file, ()
            ):
                return False
        elif name in self._dynamic_attrs.get(file, ()):
            return False

        # (b) every assignment / property-return site constructs the enum.
        key = (file, name)
        if key in seen or depth >= _MAX_PROOF_DEPTH:
            return False
        self._assignment_sites()
        assert self._attr_sites is not None and self._name_sites is not None
        if isinstance(node, ast.Name):
            sites = [
                (file, rhs)
                for rhs in self._name_sites.get(file, {}).get(name, [])
            ]
        else:
            sites = list(self._attr_sites.get(name, []))
        if not sites:
            return False
        next_seen = seen | {key}
        return all(
            self._proven_enum(rhs, site_file, target, depth + 1, next_seen)
            for site_file, rhs in sites
        )

    def _same_enum_pair(self, left: ast.AST, right: ast.AST, file: str) -> bool:
        for primary, other in ((left, right), (right, left)):
            enum = self._member_enum(primary, file)
            if enum is not None and self._proven_enum(other, file, enum):
                return True
        return False

    # -- AST diff --------------------------------------------------------

    def _diff(
        self, pinned: ast.AST | None, patched: ast.AST | None, file: str
    ) -> int | None:
        """Allowed-change count, or ``None`` when the trees differ otherwise."""
        if type(pinned) is not type(patched):
            return None
        if isinstance(pinned, ast.Compare):
            a, b = pinned, patched  # type: ignore[assignment]
            if len(a.ops) != len(b.ops) or len(a.comparators) != len(b.comparators):
                return None
            flips = self._diff(a.left, b.left, file)
            if flips is None:
                return None
            for ca, cb in zip(a.comparators, b.comparators):
                counted = self._diff(ca, cb, file)
                if counted is None:
                    return None
                flips += counted
            for index, (op_a, op_b) in enumerate(zip(a.ops, b.ops)):
                if type(op_a) is type(op_b):
                    continue
                if (type(op_a), type(op_b)) not in _FLIPS:
                    return None
                left_operand = a.left if index == 0 else a.comparators[index - 1]
                right_operand = a.comparators[index]
                if not self._same_enum_pair(left_operand, right_operand, file):
                    return None
                flips += 1
            return flips
        if isinstance(pinned, ast.AST):
            assert isinstance(patched, ast.AST)
            flips = 0
            for field in pinned._fields:
                value_a = getattr(pinned, field, None)
                value_b = getattr(patched, field, None)
                if isinstance(value_a, list) and isinstance(value_b, list):
                    if len(value_a) != len(value_b):
                        return None
                    for item_a, item_b in zip(value_a, value_b):
                        counted = self._diff(item_a, item_b, file)
                        if counted is None:
                            return None
                        flips += counted
                else:
                    counted = self._diff(value_a, value_b, file)
                    if counted is None:
                        return None
                    flips += counted
            return flips
        return 0 if pinned == patched else None

    # -- public entry ----------------------------------------------------

    def is_neutral(self, pinned_text: str, file_patch: FilePatch, path: str) -> bool:
        """True when every semantic change is an enum-member ``is``/``==`` flip."""
        try:
            patched_text = neutral.reconstruct(pinned_text, file_patch)
        except neutral.NeutralityError:
            return False
        if any(_CODING.search(line) for line in patched_text.splitlines()[:2]):
            return False
        try:
            pinned_tree = ast.parse(pinned_text)
            patched_tree = ast.parse(patched_text)
        except (SyntaxError, ValueError):
            return False

        # Cheap pre-check before paying for the whole-source index: both trees
        # parse, so look for an op-type difference on a Compare pair.
        if not _has_comparison_flip(pinned_tree, patched_tree):
            return False
        self._load()
        flips = self._diff(pinned_tree, patched_tree, path)
        return flips is not None and flips > 0


def _has_comparison_flip(pinned: ast.AST, patched: ast.AST) -> bool:
    """True when some aligned Compare pair differs only in a possible op flip.

    Conservative syntactic pre-filter: it may return True when the real answer
    is False, but never the other way around.
    """
    if type(pinned) is not type(patched):
        return False
    if not isinstance(pinned, ast.AST) or not isinstance(patched, ast.AST):
        return False
    if isinstance(pinned, ast.Compare):
        assert isinstance(patched, ast.Compare)
        if len(pinned.ops) == len(patched.ops):
            for op_a, op_b in zip(pinned.ops, patched.ops):
                if (type(op_a), type(op_b)) in _FLIPS:
                    return True
    for field in pinned._fields:
        value_a = getattr(pinned, field, None)
        value_b = getattr(patched, field, None)
        if isinstance(value_a, list) and isinstance(value_b, list):
            if len(value_a) == len(value_b) and any(
                _has_comparison_flip(item_a, item_b)
                for item_a, item_b in zip(value_a, value_b)
            ):
                return True
        elif isinstance(value_a, ast.AST) and isinstance(value_b, ast.AST):
            if _has_comparison_flip(value_a, value_b):
                return True
    return False
