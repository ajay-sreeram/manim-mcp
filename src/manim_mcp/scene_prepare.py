"""Prepare a user scene for narration: inject helpers, optionally retime calls.

Two paths:

1. **Explicit timeline** -- the user calls ``tl = narration_timeline(self)``
   and binds beats with ``tl.play_segment(i, ...)`` / ``tl.wait_segment(i)``.
   We just inject the helper source. Sync is exact at render time.

2. **Implicit retiming** -- the user wrote a normal scene with ``self.play``
   and ``self.wait``. We walk ``construct``, allocate the planned segment
   durations across each timed call (respecting static loop counts), and
   rewrite each call to consume one duration from
   ``MANIM_MCP_CALL_DURATIONS`` via ``_manim_mcp_next_duration(default)``.

Both paths also get ``fit_to_safe_frame`` / ``keep_in_safe_frame`` injected.
"""

from __future__ import annotations

import ast
from typing import Any

from .config import (
    MIN_PLAY_RUN_TIME_SECONDS,
    MIN_WAIT_DURATION_SECONDS,
    TIMELINE_SEGMENT_METHODS,
    TERM_STOPWORDS,
    VISUAL_TERM_IGNORE,
    WORD_RE,
    NarrationSyncMode,
)
from .safety import (
    sanitize_reserved_construct_bindings,
    sanitize_reserved_narration_names,
    target_construct_function,
)
from .scene_helpers import insert_after_future_imports, narration_timing_helper_source


# ---------------------------------------------------------------------------
# Static iteration count helpers (used to unroll deterministic for-loops)
# ---------------------------------------------------------------------------

def _evaluate_static_int(node: ast.AST, sequence_lengths: dict[str, int]) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len":
        if node.args:
            return _static_iteration_count(node.args[0], sequence_lengths)
    return None


def _static_iteration_count(node: ast.AST, sequence_lengths: dict[str, int] | None = None) -> int | None:
    sequence_lengths = sequence_lengths or {}
    if isinstance(node, ast.Name) and sequence_lengths is not None:
        return sequence_lengths.get(node.id)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return len(node.elts)
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        return len(node.value)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == "range":
            values: list[int] = []
            for arg in node.args:
                resolved = _evaluate_static_int(arg, sequence_lengths)
                if resolved is None:
                    return None
                values.append(resolved)
            if len(values) == 1:
                start, stop, step = 0, values[0], 1
            elif len(values) == 2:
                start, stop = values
                step = 1
            elif len(values) == 3:
                start, stop, step = values
            else:
                return None
            if step == 0:
                return None
            return max(0, len(range(start, stop, step)))
        if node.func.id in {"enumerate", "list", "tuple"} and node.args:
            return _static_iteration_count(node.args[0], sequence_lengths)
    return None


# ---------------------------------------------------------------------------
# Detecting + collecting timed calls
# ---------------------------------------------------------------------------

def _self_timed_call_kind(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if not isinstance(node.func.value, ast.Name) or node.func.value.id != "self":
        return None
    if node.func.attr in {"play", "wait"}:
        return node.func.attr
    return None


def _timed_call_weight(call: ast.Call, kind: str) -> float:
    if kind == "wait":
        if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, int | float):
            return max(float(call.args[0].value), MIN_WAIT_DURATION_SECONDS)
        for keyword in call.keywords:
            if (
                keyword.arg == "duration"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, int | float)
            ):
                return max(float(keyword.value.value), MIN_WAIT_DURATION_SECONDS)
        return 1.0

    for keyword in call.keywords:
        if (
            keyword.arg == "run_time"
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, int | float)
        ):
            return max(float(keyword.value.value), MIN_PLAY_RUN_TIME_SECONDS)
    return 1.0


class _TimedCallCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.dynamic_loop_timed_calls = 0
        self.static_loop_timed_calls = 0
        self._dynamic_loop_depth = 0
        self._static_loop_depth = 0
        self._sequence_lengths: dict[str, int] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        length = _static_iteration_count(node.value, self._sequence_lengths)
        for target in node.targets:
            if isinstance(target, ast.Name):
                if length is None:
                    self._sequence_lengths.pop(target.id, None)
                else:
                    self._sequence_lengths[target.id] = length

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
            length = _static_iteration_count(node.value, self._sequence_lengths)
            if isinstance(node.target, ast.Name):
                if length is None:
                    self._sequence_lengths.pop(node.target.id, None)
                else:
                    self._sequence_lengths[node.target.id] = length

    def visit_For(self, node: ast.For) -> None:
        static_count = (
            None
            if self._dynamic_loop_depth
            else _static_iteration_count(node.iter, self._sequence_lengths)
        )
        if static_count is None:
            self._dynamic_loop_depth += 1
            for statement in node.body:
                self.visit(statement)
            self._dynamic_loop_depth -= 1
        else:
            self._static_loop_depth += 1
            for _ in range(static_count):
                for statement in node.body:
                    self.visit(statement)
            self._static_loop_depth -= 1
        for statement in node.orelse:
            self.visit(statement)

    def visit_If(self, node: ast.If) -> None:
        # Only one branch runs at runtime; reserve enough slots for the larger
        # branch so we don't double-count timed calls.
        self.visit(node.test)
        body_calls = _TimedCallCollector()
        body_calls._sequence_lengths = dict(self._sequence_lengths)
        for statement in node.body:
            body_calls.visit(statement)
        else_calls = _TimedCallCollector()
        else_calls._sequence_lengths = dict(self._sequence_lengths)
        for statement in node.orelse:
            else_calls.visit(statement)
        # Choose the branch with more calls so the LLM's heavier path stays in sync.
        chosen = body_calls if len(body_calls.calls) >= len(else_calls.calls) else else_calls
        self.calls.extend(chosen.calls)
        self.dynamic_loop_timed_calls += chosen.dynamic_loop_timed_calls
        self.static_loop_timed_calls += chosen.static_loop_timed_calls

    def visit_Call(self, node: ast.Call) -> None:
        kind = _self_timed_call_kind(node)
        if kind:
            if self._dynamic_loop_depth:
                self.dynamic_loop_timed_calls += 1
                return
            if self._static_loop_depth:
                self.static_loop_timed_calls += 1
            self.calls.append(
                {
                    "kind": kind,
                    "weight": _timed_call_weight(node, kind),
                }
            )
        self.generic_visit(node)


class _TimedCallTransformer(ast.NodeTransformer):
    def __init__(self, calls: list[dict[str, Any]], durations: list[float]) -> None:
        self.calls = calls
        self.durations = durations
        self._dynamic_loop_depth = 0
        self._sequence_lengths: dict[str, int] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        return node

    def visit_Lambda(self, node: ast.Lambda) -> ast.AST:
        return node

    def visit_Assign(self, node: ast.Assign) -> ast.AST:
        node.value = self.visit(node.value)
        length = _static_iteration_count(node.value, self._sequence_lengths)
        for target in node.targets:
            if isinstance(target, ast.Name):
                if length is None:
                    self._sequence_lengths.pop(target.id, None)
                else:
                    self._sequence_lengths[target.id] = length
        return node

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST:
        if node.value is not None:
            node.value = self.visit(node.value)
            length = _static_iteration_count(node.value, self._sequence_lengths)
            if isinstance(node.target, ast.Name):
                if length is None:
                    self._sequence_lengths.pop(node.target.id, None)
                else:
                    self._sequence_lengths[node.target.id] = length
        return node

    def visit_For(self, node: ast.For) -> ast.AST:
        static_count = (
            None
            if self._dynamic_loop_depth
            else _static_iteration_count(node.iter, self._sequence_lengths)
        )
        if static_count is None:
            self._dynamic_loop_depth += 1
            node.body = [self.visit(statement) for statement in node.body]
            self._dynamic_loop_depth -= 1
        else:
            node.body = [self.visit(statement) for statement in node.body]
        node.iter = self.visit(node.iter)
        node.target = self.visit(node.target)
        node.orelse = [self.visit(statement) for statement in node.orelse]
        return node

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        kind = _self_timed_call_kind(node)
        if self._dynamic_loop_depth or not kind or not self.durations:
            return node

        default_duration = ast.Constant(_timed_call_weight(node, kind))
        duration_call = ast.Call(
            func=ast.Name(id="_manim_mcp_next_duration", ctx=ast.Load()),
            args=[default_duration],
            keywords=[],
        )
        if kind == "play":
            node.keywords = [keyword for keyword in node.keywords if keyword.arg != "run_time"]
            node.keywords.append(ast.keyword(arg="run_time", value=duration_call))
        else:
            node.args = [duration_call, *node.args[1:]]
            node.keywords = [keyword for keyword in node.keywords if keyword.arg != "duration"]
        return node


# ---------------------------------------------------------------------------
# Detecting explicit narration_timeline usage
# ---------------------------------------------------------------------------

class _NarrationTimelineUsageVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.call_count = 0
        self.segment_call_count = 0
        self.segment_indices: set[int] = set()
        self.dynamic_segment_call_count = 0

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == "narration_timeline":
            self.call_count += 1
        elif isinstance(node.func, ast.Attribute) and node.func.attr in {
            "play_segment",
            "wait_segment",
            "duration",
            "post_gap",
            "segment",
        }:
            self.call_count += 1
            if node.func.attr in {"play_segment", "wait_segment"}:
                self.segment_call_count += 1
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, int):
                    self.segment_indices.add(node.args[0].value)
                else:
                    self.dynamic_segment_call_count += 1
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# Per-call duration allocation across narration segments
# ---------------------------------------------------------------------------

def allocate_timed_call_durations(
    calls: list[dict[str, Any]],
    timing_plan: dict[str, Any],
) -> list[float]:
    """Distribute each segment's *visual block* (spoken + post-gap) over timed calls."""
    segment_blocks = [
        float(segment.get("block_seconds", segment["duration_seconds"] + segment.get("post_gap_seconds", 0.0)))
        for segment in timing_plan.get("segments", [])
    ]
    call_count = len(calls)
    segment_count = len(segment_blocks)
    if call_count == 0 or segment_count == 0:
        return []

    durations = [0.0] * call_count
    if call_count <= segment_count:
        for segment_index, segment_duration in enumerate(segment_blocks):
            call_index = min(call_count - 1, int(segment_index * call_count / segment_count))
            durations[call_index] += segment_duration
        return [round(max(MIN_PLAY_RUN_TIME_SECONDS, duration), 3) for duration in durations]

    groups: dict[int, list[int]] = {segment_index: [] for segment_index in range(segment_count)}
    for call_index in range(call_count):
        segment_index = min(segment_count - 1, int(call_index * segment_count / call_count))
        groups[segment_index].append(call_index)

    for segment_index, call_indices in groups.items():
        if not call_indices:
            continue
        total_weight = sum(max(float(calls[index].get("weight", 1.0)), 0.05) for index in call_indices)
        for call_index in call_indices:
            weight = max(float(calls[call_index].get("weight", 1.0)), 0.05)
            durations[call_index] = segment_blocks[segment_index] * weight / total_weight
    return [round(max(MIN_PLAY_RUN_TIME_SECONDS, duration), 3) for duration in durations]


# ---------------------------------------------------------------------------
# Visual-narration alignment heuristic (improved)
# ---------------------------------------------------------------------------

def _normalize_term(term: str) -> str | None:
    cleaned = term.lower().strip("_-'")
    if not cleaned or cleaned in TERM_STOPWORDS or cleaned in VISUAL_TERM_IGNORE:
        return None
    if len(cleaned) < 3 and not cleaned.isdigit():
        return None
    if cleaned.endswith("'s"):
        cleaned = cleaned[:-2]
    if len(cleaned) > 5 and cleaned.endswith("ies"):
        cleaned = f"{cleaned[:-3]}y"
    elif len(cleaned) > 6 and cleaned.endswith("ing"):
        cleaned = cleaned[:-3]
    elif len(cleaned) > 5 and cleaned.endswith("es"):
        cleaned = cleaned[:-2]
    elif len(cleaned) > 4 and cleaned.endswith("s"):
        cleaned = cleaned[:-1]
    if cleaned in TERM_STOPWORDS or cleaned in VISUAL_TERM_IGNORE:
        return None
    return cleaned


def _term_tokens(text: str, *, ignore_visual_boilerplate: bool = False) -> set[str]:
    ignored = VISUAL_TERM_IGNORE if ignore_visual_boilerplate else TERM_STOPWORDS
    tokens: set[str] = set()
    for raw in WORD_RE.findall(text):
        token = _normalize_term(raw)
        if token and token not in ignored:
            tokens.add(token)
    return tokens


def _identifier_terms(name: str) -> set[str]:
    import re as _re

    spaced = _re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name.replace("_", " "))
    return _term_tokens(spaced, ignore_visual_boilerplate=True)


class _VisualTermVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.string_tokens: set[str] = set()
        self.identifier_tokens: set[str] = set()
        self.referenced_names: set[str] = set()

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self.string_tokens.update(_term_tokens(node.value, ignore_visual_boilerplate=True))

    def visit_Name(self, node: ast.Name) -> None:
        self.identifier_tokens.update(_identifier_terms(node.id))
        if isinstance(node.ctx, ast.Load):
            self.referenced_names.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.identifier_tokens.update(_identifier_terms(node.attr))
        self.generic_visit(node)


def _string_tokens_for_assignment(node: ast.AST) -> set[str]:
    """Collect strings appearing inside a single top-level construct statement."""
    tokens: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            tokens.update(_term_tokens(child.value, ignore_visual_boilerplate=True))
    return tokens


def _visual_terms_for_chunk(
    chunk: list[ast.AST],
    name_string_index: dict[str, set[str]],
) -> dict[str, Any]:
    visitor = _VisualTermVisitor()
    for node in chunk:
        visitor.visit(node)

    referenced_strings: set[str] = set()
    for name in visitor.referenced_names:
        referenced_strings.update(name_string_index.get(name, set()))

    visual_tokens = sorted(visitor.string_tokens | visitor.identifier_tokens | referenced_strings)
    return {
        "string_terms": sorted(visitor.string_tokens | referenced_strings),
        "identifier_terms": sorted(visitor.identifier_tokens),
        "terms": visual_tokens,
    }


def _timeline_segment_call_infos(node: ast.AST) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if not isinstance(child.func, ast.Attribute):
            continue
        method = child.func.attr
        if method not in TIMELINE_SEGMENT_METHODS:
            continue
        if not child.args:
            continue
        index_node = child.args[0]
        if not isinstance(index_node, ast.Constant) or not isinstance(index_node.value, int):
            continue
        calls.append(
            {
                "method": method,
                "segment_index": index_node.value,
                "line": getattr(child, "lineno", None),
            }
        )
    calls.sort(key=lambda item: item["line"] or 0)
    return calls


def _build_name_string_index(construct: ast.FunctionDef) -> dict[str, set[str]]:
    """Build {name -> {string tokens used in its definition}} so later beats
    that just reference ``name`` still get credit for the strings inside it."""
    index: dict[str, set[str]] = {}
    for statement in construct.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    index.setdefault(target.id, set()).update(_string_tokens_for_assignment(statement.value))
        elif isinstance(statement, ast.AnnAssign):
            if isinstance(statement.target, ast.Name) and statement.value is not None:
                index.setdefault(statement.target.id, set()).update(
                    _string_tokens_for_assignment(statement.value)
                )
    return index


def analyze_timeline_visual_alignment(
    tree: ast.Module,
    scene_name: str,
    timing_plan: dict[str, Any],
) -> dict[str, Any]:
    """Statically compare explicit timeline visual beats with narration text."""
    construct = target_construct_function(tree, scene_name)
    segments = timing_plan.get("segments") or []
    if construct is None or not segments:
        return {"checked": False, "issues": []}

    name_string_index = _build_name_string_index(construct)
    narration_tokens = [
        _term_tokens(str(segment.get("text", "")), ignore_visual_boilerplate=False)
        for segment in segments
    ]
    beats: list[dict[str, Any]] = []
    chunk: list[ast.AST] = []
    for statement in construct.body:
        chunk.append(statement)
        segment_calls = _timeline_segment_call_infos(statement)
        if not segment_calls:
            continue
        terms = _visual_terms_for_chunk(chunk, name_string_index)
        for call in segment_calls:
            if call["method"] != "play_segment":
                continue
            index = int(call["segment_index"])
            if index < 0 or index >= len(segments):
                continue
            beat_terms = set(terms["string_terms"] or terms["terms"])
            current_overlap = sorted(beat_terms & narration_tokens[index])
            previous_overlap = (
                sorted(beat_terms & narration_tokens[index - 1]) if index > 0 else []
            )
            next_overlap = (
                sorted(beat_terms & narration_tokens[index + 1])
                if index + 1 < len(narration_tokens)
                else []
            )
            beats.append(
                {
                    "segment_index": index,
                    "line": call["line"],
                    "string_term_count": len(terms["string_terms"]),
                    "visual_terms": sorted(beat_terms)[:24],
                    "narration_terms": sorted(narration_tokens[index])[:24],
                    "overlap_terms": current_overlap[:12],
                    "overlap_count": len(current_overlap),
                    "previous_overlap_count": len(previous_overlap),
                    "next_overlap_count": len(next_overlap),
                    "next_overlap_terms": next_overlap[:12],
                    "previous_overlap_terms": previous_overlap[:12],
                }
            )
        chunk = []

    issues: list[dict[str, Any]] = []
    for beat in beats:
        index = int(beat["segment_index"])
        visual_terms = beat["visual_terms"]
        current_overlap_count = int(beat["overlap_count"])
        previous_overlap_count = int(beat["previous_overlap_count"])
        next_overlap_count = int(beat["next_overlap_count"])

        if (
            next_overlap_count >= current_overlap_count + 2
            and next_overlap_count >= 2
            and index + 1 < len(segments)
        ):
            issues.append(
                {
                    "severity": "error",
                    "code": "timeline_visual_appears_early",
                    "segment_index": index,
                    "line": beat["line"],
                    "message": (
                        f"Timeline segment {index} visually matches narration segment {index + 1} "
                        "better than its own sentence. Move that animation to the sentence that "
                        "describes it, or rewrite the narration so segment text and visuals match."
                    ),
                    "visual_terms": visual_terms[:12],
                    "current_overlap_terms": beat["overlap_terms"],
                    "next_overlap_terms": beat["next_overlap_terms"],
                }
            )
            continue

        if (
            previous_overlap_count >= current_overlap_count + 2
            and previous_overlap_count >= 2
            and index > 0
        ):
            issues.append(
                {
                    "severity": "error",
                    "code": "timeline_visual_appears_late",
                    "segment_index": index,
                    "line": beat["line"],
                    "message": (
                        f"Timeline segment {index} visually matches narration segment {index - 1} "
                        "better than its own sentence. Bind each visual beat to the narration "
                        "sentence currently being spoken."
                    ),
                    "visual_terms": visual_terms[:12],
                    "current_overlap_terms": beat["overlap_terms"],
                    "previous_overlap_terms": beat["previous_overlap_terms"],
                }
            )
            continue

        if (
            current_overlap_count < 2
            and int(beat["string_term_count"]) >= 4
            and len(visual_terms) >= 4
            and len(beat["narration_terms"]) >= 5
        ):
            issues.append(
                {
                    "severity": "warning",
                    "code": "low_visual_narration_overlap",
                    "segment_index": index,
                    "line": beat["line"],
                    "message": (
                        f"Timeline segment {index} has little textual overlap between its "
                        "visual terms and narration. This may be fine for abstract motion, "
                        "but check that the animation depicts the sentence being spoken."
                    ),
                    "visual_terms": visual_terms[:12],
                    "narration_terms": beat["narration_terms"][:12],
                    "overlap_terms": beat["overlap_terms"],
                }
            )

    return {
        "checked": True,
        "beat_count": len(beats),
        "issues": issues,
        "beats": beats,
    }


# ---------------------------------------------------------------------------
# Public: prepare_narrated_scene_code
# ---------------------------------------------------------------------------

def prepare_narrated_scene_code(
    code: str,
    *,
    scene_name: str,
    timing_plan: dict[str, Any],
    sync_mode: NarrationSyncMode,
) -> tuple[str, dict[str, Any]]:
    """Return (rewritten_source, sync_report)."""
    helper = narration_timing_helper_source(timing_plan)
    report: dict[str, Any] = {
        "sync_mode": sync_mode,
        "helper_injected": True,
        "scene_retimed": False,
        "timed_call_count": 0,
        "allocated_seconds": 0.0,
    }

    if sync_mode != "timeline":
        return insert_after_future_imports(code, helper), report

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        report["warning"] = f"Could not parse scene for automatic retiming: {exc}"
        return insert_after_future_imports(code, helper), report

    reserved_renames = sanitize_reserved_narration_names(tree)
    construct = target_construct_function(tree, scene_name)
    if construct is not None:
        reserved_renames.extend(sanitize_reserved_construct_bindings(construct))
    if reserved_renames:
        report["reserved_helper_name_renames"] = reserved_renames

    timeline_usage = _NarrationTimelineUsageVisitor()
    timeline_usage.visit(tree)
    report["explicit_timeline_call_count"] = timeline_usage.call_count
    report["explicit_timeline_used"] = timeline_usage.call_count > 0

    segment_count = len(timing_plan.get("segments") or [])
    if report["explicit_timeline_used"]:
        covered_indices = sorted(
            index for index in timeline_usage.segment_indices if 0 <= index < segment_count
        )
        out_of_range_indices = sorted(
            index for index in timeline_usage.segment_indices if index < 0 or index >= segment_count
        )
        missing_indices = [
            index for index in range(segment_count) if index not in timeline_usage.segment_indices
        ]
        report["explicit_timeline_segment_count"] = segment_count
        report["explicit_timeline_segment_call_count"] = timeline_usage.segment_call_count
        report["explicit_timeline_segment_indices"] = covered_indices
        report["explicit_timeline_out_of_range_segments"] = out_of_range_indices
        report["explicit_timeline_covered_segment_count"] = len(set(covered_indices))
        report["explicit_timeline_missing_segments"] = missing_indices
        report["explicit_timeline_dynamic_segment_call_count"] = timeline_usage.dynamic_segment_call_count
        report["explicit_timeline_coverage_ratio"] = (
            round(len(set(covered_indices)) / segment_count, 3) if segment_count else 1.0
        )
        report["timeline_alignment"] = analyze_timeline_visual_alignment(
            tree,
            scene_name,
            timing_plan,
        )

    if construct is None:
        report["warning"] = f"Could not find construct() for scene {scene_name!r}; using global video fit only."
        ast.fix_missing_locations(tree)
        return insert_after_future_imports(ast.unparse(tree), helper), report

    collector = _TimedCallCollector()
    for statement in construct.body:
        collector.visit(statement)
    report["timed_call_count"] = len(collector.calls)
    report["dynamic_loop_timed_call_count"] = collector.dynamic_loop_timed_calls
    report["static_loop_timed_call_count"] = collector.static_loop_timed_calls

    if report["explicit_timeline_used"]:
        if collector.calls or collector.dynamic_loop_timed_calls:
            outside_timeline_seconds = round(
                sum(max(float(call.get("weight", 1.0)), 0.05) for call in collector.calls),
                3,
            )
            report["outside_timeline_timed_call_count"] = len(collector.calls)
            report["outside_timeline_estimated_seconds"] = outside_timeline_seconds
            report["warning"] = (
                "Scene uses narration_timeline(...); ordinary self.play/self.wait calls "
                "were left unchanged. Keep those calls short or move them into "
                "tl.play_segment(...) / tl.wait_segment(...)."
            )
        else:
            report["warning"] = "Scene uses narration_timeline(...); no automatic self.play/self.wait retiming was needed."
        ast.fix_missing_locations(tree)
        return insert_after_future_imports(ast.unparse(tree), helper), report

    durations = allocate_timed_call_durations(collector.calls, timing_plan)
    report["allocated_seconds"] = round(sum(durations), 3)
    report["call_durations_seconds"] = durations
    if collector.dynamic_loop_timed_calls:
        report["dynamic_loop_warning"] = (
            "Some timed calls were inside loops with unknown iteration counts; "
            "those calls were left unchanged and the final video fit handles residual sync."
        )

    if not durations:
        report["warning"] = "No self.play(...) or self.wait(...) calls found; using global video fit only."
        ast.fix_missing_locations(tree)
        return insert_after_future_imports(ast.unparse(tree), helper), report

    transformer = _TimedCallTransformer(collector.calls, durations)
    construct.body = [transformer.visit(statement) for statement in construct.body]
    ast.fix_missing_locations(tree)
    report["scene_retimed"] = True
    helper = narration_timing_helper_source(timing_plan, call_durations=durations)
    return insert_after_future_imports(ast.unparse(tree), helper), report
