"""Static analysis of user-submitted Manim scene code.

Three jobs:

1. Block obviously dangerous imports/calls before we ever shell out to Manim.
2. Catch a small class of generated-code NameErrors and obvious animation-kwarg
   mistakes that Manim would otherwise raise mid-render.
3. Rename user-defined names that would shadow the helpers we inject
   (``narration_timeline``, ``fit_to_safe_frame``, etc.).
"""

from __future__ import annotations

import ast
import builtins
from typing import Any

from .config import (
    BLOCKED_DIRECT_CALLS,
    BLOCKED_MODULE_ATTR_CALLS,
    BLOCKED_MODULES,
    RESERVED_NARRATION_HELPER_NAMES,
    SCENE_BASE_NAMES,
)


# ---------------------------------------------------------------------------
# Safety preflight
# ---------------------------------------------------------------------------

class _SafetyVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[str] = []
        self.blocked_aliases: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".", 1)[0]
            if root in BLOCKED_MODULES:
                self.blocked_aliases.add(alias.asname or root)
                self.violations.append(f"line {node.lineno}: blocked import '{alias.name}'")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        root = module.split(".", 1)[0]
        if root in BLOCKED_MODULES:
            for alias in node.names:
                self.blocked_aliases.add(alias.asname or alias.name)
            self.violations.append(f"line {node.lineno}: blocked import from '{module}'")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in BLOCKED_DIRECT_CALLS:
            self.violations.append(f"line {node.lineno}: blocked call '{node.func.id}'")

        if isinstance(node.func, ast.Attribute):
            root_name = root_name_of(node.func.value)
            if root_name in self.blocked_aliases and node.func.attr in BLOCKED_MODULE_ATTR_CALLS:
                self.violations.append(
                    f"line {node.lineno}: blocked call '{root_name}.{node.func.attr}'"
                )
        self.generic_visit(node)


def analyze_code_safety(code: str) -> list[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"line {exc.lineno or '?'}: syntax error: {exc.msg}"]
    visitor = _SafetyVisitor()
    visitor.visit(tree)
    return visitor.violations


# ---------------------------------------------------------------------------
# Name validation (catches a few common LLM mistakes pre-render)
# ---------------------------------------------------------------------------

def root_name_of(node: ast.AST) -> str | None:
    current = node
    while isinstance(current, ast.Attribute):
        current = current.value
    if isinstance(current, ast.Name):
        return current.id
    return None


def base_name_of(base: ast.AST) -> str | None:
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    if isinstance(base, ast.Subscript):
        return base_name_of(base.value)
    return None


def _bound_names_from_target(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for element in target.elts:
            names.update(_bound_names_from_target(element))
        return names
    return set()


def _module_bound_names(tree: ast.Module) -> set[str]:
    names: set[str] = set(dir(builtins))
    names.update(RESERVED_NARRATION_HELPER_NAMES)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module == "manim" and any(alias.name == "*" for alias in node.names):
                names.add("__manim_star__")
            for alias in node.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_bound_names_from_target(target))
        elif isinstance(node, ast.AnnAssign):
            names.update(_bound_names_from_target(node.target))
    return names


def scene_classes(tree: ast.Module) -> tuple[list[str], list[str]]:
    all_classes: list[str] = []
    scene_subclasses: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            all_classes.append(node.name)
            if any((base_name_of(base) in SCENE_BASE_NAMES) for base in node.bases):
                scene_subclasses.append(node.name)
    return all_classes, scene_subclasses


def infer_scene_name(code: str, scene_name: str | None = None) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ValueError(f"Syntax error on line {exc.lineno or '?'}: {exc.msg}") from exc

    all_classes, subclasses = scene_classes(tree)
    if scene_name:
        if all_classes and scene_name not in all_classes:
            raise ValueError(f"Scene '{scene_name}' was not found in the submitted code.")
        if subclasses and scene_name not in subclasses:
            raise ValueError(f"Class '{scene_name}' is not a recognized Manim Scene subclass.")
        return scene_name

    if len(subclasses) == 1:
        return subclasses[0]
    if not subclasses:
        raise ValueError("Could not infer a scene name because no Scene subclass was found.")
    raise ValueError(
        "Could not infer a scene name because multiple Scene subclasses were found: "
        + ", ".join(subclasses)
    )


def target_construct_function(tree: ast.Module, scene_name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != scene_name:
            continue
        for child in node.body:
            if isinstance(child, ast.FunctionDef) and child.name == "construct":
                return child
    return None


class _ConstructNameValidator(ast.NodeVisitor):
    """Catch a small class of generated-code NameErrors before rendering."""

    def __init__(self, initial_names: set[str]) -> None:
        self.defined = set(initial_names)
        self.violations: list[str] = []
        self._expired_comprehension_targets: set[str] = set()
        self._reported: set[tuple[int, str]] = set()

    def _report(self, node: ast.AST, name: str, message: str) -> None:
        line = getattr(node, "lineno", 0) or 0
        key = (line, name)
        if key in self._reported:
            return
        self._reported.add(key)
        self.violations.append(f"line {line}: {message}")

    def _bind_target(self, target: ast.AST) -> None:
        self.defined.update(_bound_names_from_target(target))

    def visit_Name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Load):
            return
        if node.id in self.defined:
            return
        if node.id in self._expired_comprehension_targets:
            self._report(
                node,
                node.id,
                (
                    f"name '{node.id}' is not defined outside the comprehension that used it; "
                    "assign from an explicit list element or loop over the list instead"
                ),
            )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.defined.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.defined.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.defined.add(node.name)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._bind_target(target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
        self._bind_target(node.target)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.target)
        self.visit(node.value)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self._bind_target(node.target)
        for statement in node.body:
            self.visit(statement)
        for statement in node.orelse:
            self.visit(statement)

    def _visit_comprehension(self, generators: list[ast.comprehension], value_nodes: list[ast.AST]) -> None:
        saved_defined = set(self.defined)
        local_targets: set[str] = set()
        for generator in generators:
            self.visit(generator.iter)
            target_names = _bound_names_from_target(generator.target)
            self.defined.update(target_names)
            local_targets.update(target_names)
            for condition in generator.ifs:
                self.visit(condition)
        for value_node in value_nodes:
            self.visit(value_node)
        self.defined = saved_defined
        self._expired_comprehension_targets.update(local_targets)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, [node.key, node.value])


# ---------------------------------------------------------------------------
# Animation-kwarg sanity check (catches the very common
# "Animation.__init__() got an unexpected keyword argument 'color'" failure)
# ---------------------------------------------------------------------------

# Animations whose __init__ accepts *only* timing-shaped kwargs. Style kwargs
# (color, fill_opacity, stroke_width, font_size, ...) belong on the *mobject*,
# not on the animation. We are conservative: this list only contains the
# animation classes whose API is well known not to accept style kwargs.
_STYLE_FORBIDDING_ANIMATIONS = {
    "Create", "Uncreate", "Write", "Unwrite",
    "FadeIn", "FadeOut",
    "GrowFromCenter", "GrowFromEdge", "GrowFromPoint",
    "DrawBorderThenFill",
    "ShowPassingFlash",
    "Indicate", "Flash", "Circumscribe", "Wiggle", "FocusOn",
}

# Kwargs almost always meant for the mobject, not the animation.
_MOBJECT_STYLE_KWARGS = {
    "color", "fill_color", "fill_opacity", "stroke_color",
    "stroke_width", "stroke_opacity", "font", "font_size",
    "weight", "slant", "background_color",
}

# Allow-listed Animation kwargs; everything else flagged as a style mistake
# only triggers a *warning* (we don't want to block legitimate but unusual
# usage like Indicate(color=YELLOW), which in newer Manim does work).
_ANIMATION_VALID_KWARGS = {
    "run_time", "rate_func", "lag_ratio", "reverse_rate_function",
    "remover", "introducer", "suspend_mobject_updating",
    "use_override", "name", "group",
    # Class-specific extras for the Indicate family:
    "scale_factor", "shift", "rotate", "axis", "match_color",
    "color",  # Indicate / Flash / Circumscribe accept color in modern CE.
}


class _AnimationKwargValidator(ast.NodeVisitor):
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        func_name = None
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr

        if func_name in _STYLE_FORBIDDING_ANIMATIONS:
            for keyword in node.keywords:
                if keyword.arg is None:
                    continue
                if keyword.arg in _MOBJECT_STYLE_KWARGS and keyword.arg not in _ANIMATION_VALID_KWARGS:
                    line = getattr(node, "lineno", 0) or 0
                    self.warnings.append(
                        f"line {line}: '{func_name}(... {keyword.arg}=...)' likely fails — "
                        f"'{keyword.arg}' is a mobject style kwarg. "
                        f"Set it on the mobject (e.g. Text('hi', {keyword.arg}=...)) "
                        f"and pass only timing kwargs (run_time=, rate_func=, lag_ratio=) to {func_name}()."
                    )
        self.generic_visit(node)


def analyze_code_validation(code: str, scene_name: str) -> list[str]:
    """Return strict NameError-style violations for the named scene's construct."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    construct = target_construct_function(tree, scene_name)
    if construct is None:
        return []
    validator = _ConstructNameValidator({*_module_bound_names(tree), "self"})
    for statement in construct.body:
        validator.visit(statement)
    return validator.violations


def analyze_animation_kwargs(code: str) -> list[str]:
    """Return non-blocking warnings about animation kwarg misuse."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    validator = _AnimationKwargValidator()
    validator.visit(tree)
    return validator.warnings


def analyze_all_constructs(code: str) -> list[str]:
    """Lint every Scene subclass's construct, not just the rendered one."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    _, subclasses = scene_classes(tree)
    if len(subclasses) <= 1:
        return []
    violations: list[str] = []
    for name in subclasses:
        construct = target_construct_function(tree, name)
        if construct is None:
            continue
        validator = _ConstructNameValidator({*_module_bound_names(tree), "self"})
        for statement in construct.body:
            validator.visit(statement)
        for issue in validator.violations:
            violations.append(f"scene {name}: {issue}")
    return violations


# ---------------------------------------------------------------------------
# Reserved-name shadowing: we silently rename user bindings that collide with
# the helpers we inject, so user code never accidentally hides them.
# ---------------------------------------------------------------------------

def _reserved_user_name(name: str) -> str:
    return f"_manim_mcp_user_{name}"


def _rename_reserved_binding(name: str, *, kind: str, line: int | None) -> dict[str, Any] | None:
    if name not in RESERVED_NARRATION_HELPER_NAMES:
        return None
    return {
        "kind": kind,
        "line": line,
        "from": name,
        "to": _reserved_user_name(name),
    }


def _rename_reserved_targets(target: ast.AST, *, kind: str, line: int | None) -> list[dict[str, Any]]:
    renames: list[dict[str, Any]] = []
    if isinstance(target, ast.Name):
        rename = _rename_reserved_binding(target.id, kind=kind, line=line)
        if rename:
            target.id = rename["to"]
            renames.append(rename)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            renames.extend(_rename_reserved_targets(element, kind=kind, line=line))
    return renames


def sanitize_reserved_narration_names(tree: ast.Module) -> list[dict[str, Any]]:
    """Rename top-level user bindings that would shadow injected helpers."""
    renames: list[dict[str, Any]] = []
    for node in tree.body:
        line = getattr(node, "lineno", None)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            rename = _rename_reserved_binding(node.name, kind=type(node).__name__, line=line)
            if rename:
                node.name = rename["to"]
                renames.append(rename)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                renames.extend(_rename_reserved_targets(target, kind="Assign", line=line))
        elif isinstance(node, ast.AnnAssign):
            renames.extend(_rename_reserved_targets(node.target, kind="AnnAssign", line=line))
        elif isinstance(node, ast.AugAssign):
            renames.extend(_rename_reserved_targets(node.target, kind="AugAssign", line=line))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound_name = alias.asname or alias.name.split(".", 1)[0]
                rename = _rename_reserved_binding(bound_name, kind="Import", line=line)
                if rename:
                    alias.asname = rename["to"]
                    renames.append(rename)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound_name = alias.asname or alias.name
                rename = _rename_reserved_binding(bound_name, kind="ImportFrom", line=line)
                if rename:
                    alias.asname = rename["to"]
                    renames.append(rename)
    return renames


class _ReservedConstructBindingRenamer(ast.NodeTransformer):
    def __init__(self) -> None:
        self.renames: list[dict[str, Any]] = []

    def _maybe_rename_param(self, arg: ast.arg, line: int | None) -> None:
        rename = _rename_reserved_binding(arg.arg, kind="LocalParam", line=line)
        if rename:
            arg.arg = rename["to"]
            self.renames.append(rename)

    def _rename_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        rename = _rename_reserved_binding(
            node.name,
            kind=f"Local{type(node).__name__}",
            line=getattr(node, "lineno", None),
        )
        if rename:
            node.name = rename["to"]
            self.renames.append(rename)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self._rename_func(node)
        line = getattr(node, "lineno", None)
        for arg in [*node.args.args, *node.args.kwonlyargs, *node.args.posonlyargs]:
            self._maybe_rename_param(arg, line)
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        self._rename_func(node)
        line = getattr(node, "lineno", None)
        for arg in [*node.args.args, *node.args.kwonlyargs, *node.args.posonlyargs]:
            self._maybe_rename_param(arg, line)
        self.generic_visit(node)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        self._rename_func(node)
        return node

    def visit_Assign(self, node: ast.Assign) -> ast.AST:
        node.value = self.visit(node.value)
        for target in node.targets:
            self.renames.extend(
                _rename_reserved_targets(
                    target,
                    kind="LocalAssign",
                    line=getattr(node, "lineno", None),
                )
            )
        return node

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST:
        if node.value is not None:
            node.value = self.visit(node.value)
        self.renames.extend(
            _rename_reserved_targets(
                node.target,
                kind="LocalAnnAssign",
                line=getattr(node, "lineno", None),
            )
        )
        return node

    def visit_AugAssign(self, node: ast.AugAssign) -> ast.AST:
        node.value = self.visit(node.value)
        self.renames.extend(
            _rename_reserved_targets(
                node.target,
                kind="LocalAugAssign",
                line=getattr(node, "lineno", None),
            )
        )
        return node

    def visit_For(self, node: ast.For) -> ast.AST:
        node.iter = self.visit(node.iter)
        self.renames.extend(
            _rename_reserved_targets(
                node.target,
                kind="LocalForTarget",
                line=getattr(node, "lineno", None),
            )
        )
        node.body = [self.visit(statement) for statement in node.body]
        node.orelse = [self.visit(statement) for statement in node.orelse]
        return node

    def visit_Import(self, node: ast.Import) -> ast.AST:
        line = getattr(node, "lineno", None)
        for alias in node.names:
            bound_name = alias.asname or alias.name.split(".", 1)[0]
            rename = _rename_reserved_binding(bound_name, kind="LocalImport", line=line)
            if rename:
                alias.asname = rename["to"]
                self.renames.append(rename)
        return node

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST:
        line = getattr(node, "lineno", None)
        for alias in node.names:
            if alias.name == "*":
                continue
            bound_name = alias.asname or alias.name
            rename = _rename_reserved_binding(bound_name, kind="LocalImportFrom", line=line)
            if rename:
                alias.asname = rename["to"]
                self.renames.append(rename)
        return node


def sanitize_reserved_construct_bindings(construct: ast.FunctionDef) -> list[dict[str, Any]]:
    renamer = _ReservedConstructBindingRenamer()
    construct.body = [renamer.visit(statement) for statement in construct.body]
    return renamer.renames
