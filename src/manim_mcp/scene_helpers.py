"""The runtime helper that we inject at the top of every narrated scene.

This module *generates* a Python source string. It does not run anything
itself. The string is added to the user's scene script before Manim renders
it, so all of the names below (``narration_timeline``, ``NarrationTimeline``,
``fit_to_safe_frame``, ``keep_in_safe_frame``) become available inside the
user's ``construct(self)``.

The helper:

* exposes ``tl = narration_timeline(self)`` for explicit per-segment binding,
* automatically inserts the planned ``post_gap_seconds`` silence after each
  ``tl.play_segment(...)`` / ``tl.wait_segment(...)`` so the visual timeline
  matches the audio timeline,
* clamps ``hold`` so it never overshoots the segment,
* enforces minimum run-times (``MIN_PLAY_RUN_TIME``, ``MIN_WAIT_DURATION``)
  so animations never collapse into a 0.05s "pop",
* writes ``narration/timeline_actual.json`` after **every** event so a
  partial render still produces useful timeline data,
* provides ``fit_to_safe_frame`` and ``keep_in_safe_frame`` that handle the
  "wider than frame" edge case correctly.
"""

from __future__ import annotations

import json
from typing import Any

from .config import MIN_PLAY_RUN_TIME_SECONDS, MIN_WAIT_DURATION_SECONDS


def narration_timing_helper_source(
    timing_plan: dict[str, Any],
    call_durations: list[float] | None = None,
) -> str:
    plan_literal = json.dumps(timing_plan, indent=2)
    durations_literal = json.dumps(call_durations or [])
    min_play = MIN_PLAY_RUN_TIME_SECONDS
    min_wait = MIN_WAIT_DURATION_SECONDS

    return f'''
NARRATION_TIMING = {plan_literal}
MANIM_MCP_CALL_DURATIONS = {durations_literal}
MANIM_MCP_CALL_DURATION_INDEX = 0
MANIM_MCP_MIN_PLAY_RUN_TIME = {min_play}
MANIM_MCP_MIN_WAIT_DURATION = {min_wait}


def _manim_mcp_reset_call_index():
    """Reset the per-scene retiming counter."""
    global MANIM_MCP_CALL_DURATION_INDEX
    MANIM_MCP_CALL_DURATION_INDEX = 0


def _manim_mcp_next_duration(default=1.0):
    """Return the next measured visual-beat duration for automatic retiming."""
    global MANIM_MCP_CALL_DURATION_INDEX
    try:
        fallback = max(MANIM_MCP_MIN_PLAY_RUN_TIME, float(default))
    except Exception:
        fallback = MANIM_MCP_MIN_PLAY_RUN_TIME
    if MANIM_MCP_CALL_DURATION_INDEX >= len(MANIM_MCP_CALL_DURATIONS):
        return fallback
    duration = MANIM_MCP_CALL_DURATIONS[MANIM_MCP_CALL_DURATION_INDEX]
    MANIM_MCP_CALL_DURATION_INDEX += 1
    try:
        return max(MANIM_MCP_MIN_PLAY_RUN_TIME, float(duration))
    except Exception:
        return fallback


MANIM_MCP_TIMELINE_EVENTS = []
MANIM_MCP_OUTSIDE_TIMED_EVENTS = []
MANIM_MCP_TIMELINE_WARNINGS = []
MANIM_MCP_IN_TIMELINE_CALL = 0
MANIM_MCP_IN_OUTSIDE_WAIT = 0


def _manim_mcp_scene_time(scene):
    try:
        return float(getattr(scene, "time", 0.0))
    except Exception:
        return 0.0


def _manim_mcp_mobject_family_count(scene):
    try:
        stack = list(getattr(scene, "mobjects", []))
        count = 0
        while stack:
            mobject = stack.pop()
            count += 1
            stack.extend(list(getattr(mobject, "submobjects", [])))
        return count
    except Exception:
        return None


def _manim_mcp_top_mobject_count(scene):
    try:
        return len(list(getattr(scene, "mobjects", [])))
    except Exception:
        return None


def _manim_mcp_json_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        return float(value)
    except Exception:
        return repr(value)


def _manim_mcp_record_event(collection, event):
    try:
        event["start_seconds"] = round(float(event.get("start_seconds", 0.0)), 3)
        event["end_seconds"] = round(float(event.get("end_seconds", 0.0)), 3)
        event["actual_seconds"] = round(
            max(0.0, event["end_seconds"] - event["start_seconds"]),
            3,
        )
    except Exception:
        pass
    collection.append(event)
    _manim_mcp_write_timeline_events()


def _manim_mcp_record_warning(message, **fields):
    fields["message"] = message
    MANIM_MCP_TIMELINE_WARNINGS.append(fields)


def _manim_mcp_patch_scene_instance(scene):
    if getattr(scene, "_manim_mcp_methods_patched", False):
        return

    original_play = scene.play
    original_wait = scene.wait

    def play_wrapper(*args, **kwargs):
        if MANIM_MCP_IN_TIMELINE_CALL or MANIM_MCP_IN_OUTSIDE_WAIT:
            return original_play(*args, **kwargs)
        start = _manim_mcp_scene_time(scene)
        result = original_play(*args, **kwargs)
        end = _manim_mcp_scene_time(scene)
        mobject_count = _manim_mcp_mobject_family_count(scene)
        top_count = _manim_mcp_top_mobject_count(scene)
        _manim_mcp_record_event(
            MANIM_MCP_OUTSIDE_TIMED_EVENTS,
            dict(
                kind="play",
                line=None,
                animation_count=len(args),
                requested_run_time=_manim_mcp_json_value(kwargs.get("run_time")),
                start_seconds=start,
                end_seconds=end,
                end_mobject_family_count=mobject_count,
                end_top_mobject_count=top_count,
            ),
        )
        return result

    def wait_wrapper(*args, **kwargs):
        global MANIM_MCP_IN_OUTSIDE_WAIT
        if MANIM_MCP_IN_TIMELINE_CALL:
            return original_wait(*args, **kwargs)
        start = _manim_mcp_scene_time(scene)
        MANIM_MCP_IN_OUTSIDE_WAIT += 1
        try:
            result = original_wait(*args, **kwargs)
        finally:
            MANIM_MCP_IN_OUTSIDE_WAIT -= 1
        end = _manim_mcp_scene_time(scene)
        mobject_count = _manim_mcp_mobject_family_count(scene)
        top_count = _manim_mcp_top_mobject_count(scene)
        requested = args[0] if args else kwargs.get("duration")
        _manim_mcp_record_event(
            MANIM_MCP_OUTSIDE_TIMED_EVENTS,
            dict(
                kind="wait",
                line=None,
                requested_duration=_manim_mcp_json_value(requested),
                start_seconds=start,
                end_seconds=end,
                end_mobject_family_count=mobject_count,
                end_top_mobject_count=top_count,
            ),
        )
        return result

    scene.play = play_wrapper
    scene.wait = wait_wrapper
    scene._manim_mcp_methods_patched = True


def _manim_mcp_write_timeline_events():
    try:
        import json as _manim_mcp_json
        import os as _manim_mcp_os

        job_dir = _manim_mcp_os.environ.get("MANIM_MCP_JOB_DIR")
        if not job_dir:
            return
        narration_dir = _manim_mcp_os.path.join(job_dir, "narration")
        _manim_mcp_os.makedirs(narration_dir, exist_ok=True)
        path = _manim_mcp_os.path.join(narration_dir, "timeline_actual.json")
        all_events = MANIM_MCP_TIMELINE_EVENTS + MANIM_MCP_OUTSIDE_TIMED_EVENTS
        payload = dict(
            timing_source="manim_scene_time",
            segment_count=len(NARRATION_TIMING.get("segments", [])),
            timeline_events=MANIM_MCP_TIMELINE_EVENTS,
            outside_timed_events=MANIM_MCP_OUTSIDE_TIMED_EVENTS,
            timeline_event_count=len(MANIM_MCP_TIMELINE_EVENTS),
            outside_timed_event_count=len(MANIM_MCP_OUTSIDE_TIMED_EVENTS),
            timeline_warnings=MANIM_MCP_TIMELINE_WARNINGS,
            timeline_total_seconds=round(
                sum(float(event.get("actual_seconds", 0.0)) for event in MANIM_MCP_TIMELINE_EVENTS),
                3,
            ),
            outside_timed_total_seconds=round(
                sum(float(event.get("actual_seconds", 0.0)) for event in MANIM_MCP_OUTSIDE_TIMED_EVENTS),
                3,
            ),
            scene_time_seconds=round(
                max([float(event.get("end_seconds", 0.0)) for event in all_events] or [0.0]),
                3,
            ),
        )
        # Atomic-ish: write to a sibling and replace.
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as file:
            _manim_mcp_json.dump(payload, file, indent=2)
            file.write("\\n")
        _manim_mcp_os.replace(tmp_path, path)
    except Exception:
        return


try:
    import atexit as _manim_mcp_atexit

    _manim_mcp_atexit.register(_manim_mcp_write_timeline_events)
except Exception:
    pass


class NarrationTimeline:
    """Small helper injected by manim_mcp for narrated scenes."""

    def __init__(self, scene, plan=None):
        self.scene = scene
        self.plan = plan or NARRATION_TIMING
        _manim_mcp_patch_scene_instance(scene)

    def segment(self, index):
        try:
            index = int(index)
        except Exception:
            return dict(index=index, text="", duration_seconds=0.05,
                        post_gap_seconds=0.0, out_of_range=True)
        segments = self.plan.get("segments", [])
        if 0 <= index < len(segments):
            return segments[index]
        return dict(index=index, text="", duration_seconds=0.05,
                    post_gap_seconds=0.0, out_of_range=True)

    def duration(self, index, *, minimum=0.05):
        return max(minimum, float(self.segment(index)["duration_seconds"]))

    def post_gap(self, index):
        seg = self.segment(index)
        try:
            return max(0.0, float(seg.get("post_gap_seconds", 0.0)))
        except Exception:
            return 0.0

    def play_segment(self, index, *animations, hold=0.0, add_post_gap=True, **kwargs):
        global MANIM_MCP_IN_TIMELINE_CALL
        segment = self.segment(index)
        target_duration = max(MANIM_MCP_MIN_PLAY_RUN_TIME, float(segment["duration_seconds"]))
        post_gap = max(0.0, float(segment.get("post_gap_seconds", 0.0))) if add_post_gap else 0.0

        # Clamp hold so animation always has at least the minimum runtime.
        max_hold = max(0.0, target_duration - MANIM_MCP_MIN_PLAY_RUN_TIME)
        clamped_hold = max(0.0, min(float(hold), max_hold))
        if clamped_hold != float(hold):
            _manim_mcp_record_warning(
                "play_segment hold parameter clamped to fit segment duration",
                segment_index=int(segment.get("index", index)),
                requested_hold=float(hold),
                applied_hold=clamped_hold,
                segment_duration_seconds=target_duration,
            )
        run_time = max(MANIM_MCP_MIN_PLAY_RUN_TIME, target_duration - clamped_hold)

        start = _manim_mcp_scene_time(self.scene)
        start_mobject_count = _manim_mcp_mobject_family_count(self.scene)
        start_top_count = _manim_mcp_top_mobject_count(self.scene)
        MANIM_MCP_IN_TIMELINE_CALL += 1
        try:
            if animations:
                self.scene.play(*animations, run_time=run_time, **kwargs)
            else:
                self.scene.wait(run_time)
            if clamped_hold > 0:
                self.scene.wait(clamped_hold)
            if post_gap > 0.02:
                self.scene.wait(post_gap)
        finally:
            MANIM_MCP_IN_TIMELINE_CALL -= 1
        end = _manim_mcp_scene_time(self.scene)
        end_mobject_count = _manim_mcp_mobject_family_count(self.scene)
        end_top_count = _manim_mcp_top_mobject_count(self.scene)
        _manim_mcp_record_event(
            MANIM_MCP_TIMELINE_EVENTS,
            dict(
                kind="play_segment",
                segment_index=segment.get("index", index),
                out_of_range=bool(segment.get("out_of_range")),
                segment_text=segment.get("text", ""),
                target_duration_seconds=target_duration,
                requested_run_time=run_time,
                hold_seconds=clamped_hold,
                post_gap_seconds=post_gap,
                animation_count=len(animations),
                start_seconds=start,
                end_seconds=end,
                start_mobject_family_count=start_mobject_count,
                end_mobject_family_count=end_mobject_count,
                start_top_mobject_count=start_top_count,
                end_top_mobject_count=end_top_count,
            ),
        )

    def wait_segment(self, index, *, scale=1.0, add_post_gap=True):
        global MANIM_MCP_IN_TIMELINE_CALL
        segment = self.segment(index)
        target_duration = max(MANIM_MCP_MIN_WAIT_DURATION, self.duration(index) * max(0.1, float(scale)))
        post_gap = max(0.0, float(segment.get("post_gap_seconds", 0.0))) if add_post_gap else 0.0

        start = _manim_mcp_scene_time(self.scene)
        start_mobject_count = _manim_mcp_mobject_family_count(self.scene)
        start_top_count = _manim_mcp_top_mobject_count(self.scene)
        MANIM_MCP_IN_TIMELINE_CALL += 1
        try:
            self.scene.wait(target_duration)
            if post_gap > 0.02:
                self.scene.wait(post_gap)
        finally:
            MANIM_MCP_IN_TIMELINE_CALL -= 1
        end = _manim_mcp_scene_time(self.scene)
        end_mobject_count = _manim_mcp_mobject_family_count(self.scene)
        end_top_count = _manim_mcp_top_mobject_count(self.scene)
        _manim_mcp_record_event(
            MANIM_MCP_TIMELINE_EVENTS,
            dict(
                kind="wait_segment",
                segment_index=segment.get("index", index),
                out_of_range=bool(segment.get("out_of_range")),
                segment_text=segment.get("text", ""),
                target_duration_seconds=target_duration,
                requested_scale=scale,
                post_gap_seconds=post_gap,
                animation_count=0,
                start_seconds=start,
                end_seconds=end,
                start_mobject_family_count=start_mobject_count,
                end_mobject_family_count=end_mobject_count,
                start_top_mobject_count=start_top_count,
                end_top_mobject_count=end_top_count,
            ),
        )


def narration_timeline(scene):
    _manim_mcp_reset_call_index()
    return NarrationTimeline(scene)


def fit_to_safe_frame(mobject, *, width_ratio=0.88, height_ratio=0.82, recenter=False):
    """Scale a mobject (or VGroup) to fit inside the visible Manim frame."""
    try:
        from manim import config as _mcp_cfg
    except Exception:
        return mobject
    max_width = _mcp_cfg.frame_width * width_ratio
    max_height = _mcp_cfg.frame_height * height_ratio
    scale_factors = []
    if getattr(mobject, "width", 0) and mobject.width > max_width:
        scale_factors.append(max_width / mobject.width)
    if getattr(mobject, "height", 0) and mobject.height > max_height:
        scale_factors.append(max_height / mobject.height)
    if scale_factors:
        mobject.scale(min(scale_factors))
    if recenter:
        try:
            mobject.move_to((0, 0, 0))
        except Exception:
            pass
    return mobject


def keep_in_safe_frame(mobject, *, buff=0.35):
    """Nudge a mobject back inside the visible Manim frame; scale if too wide."""
    try:
        from manim import LEFT as _mcp_LEFT, RIGHT as _mcp_RIGHT, UP as _mcp_UP, DOWN as _mcp_DOWN, config as _mcp_cfg
    except Exception:
        return mobject
    usable_width = _mcp_cfg.frame_width - 2 * buff
    usable_height = _mcp_cfg.frame_height - 2 * buff
    width = getattr(mobject, "width", 0) or 0
    height = getattr(mobject, "height", 0) or 0
    # If the mobject is bigger than the safe area, scale it down first;
    # nudging cannot make a too-wide object fit on both sides.
    if width > usable_width or height > usable_height:
        scale_factors = []
        if width > usable_width:
            scale_factors.append(usable_width / width)
        if height > usable_height:
            scale_factors.append(usable_height / height)
        if scale_factors:
            mobject.scale(min(scale_factors) * 0.98)
    left = -_mcp_cfg.frame_width / 2 + buff
    right = _mcp_cfg.frame_width / 2 - buff
    bottom = -_mcp_cfg.frame_height / 2 + buff
    top = _mcp_cfg.frame_height / 2 - buff
    if mobject.get_left()[0] < left:
        mobject.shift(_mcp_RIGHT * (left - mobject.get_left()[0]))
    if mobject.get_right()[0] > right:
        mobject.shift(_mcp_LEFT * (mobject.get_right()[0] - right))
    if mobject.get_bottom()[1] < bottom:
        mobject.shift(_mcp_UP * (bottom - mobject.get_bottom()[1]))
    if mobject.get_top()[1] > top:
        mobject.shift(_mcp_DOWN * (mobject.get_top()[1] - top))
    return mobject
'''.strip()


def insert_after_future_imports(code: str, insertion: str) -> str:
    """Insert helper source after any ``from __future__ import ...`` lines."""
    import ast

    try:
        tree = ast.parse(code)
    except SyntaxError:
        tree = None

    if tree is not None:
        insert_line = 0
        for index, node in enumerate(tree.body):
            is_module_docstring = (
                index == 0
                and isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            )
            is_future_import = isinstance(node, ast.ImportFrom) and node.module == "__future__"
            if not is_module_docstring and not is_future_import:
                break
            insert_line = getattr(node, "end_lineno", getattr(node, "lineno", 0))
        lines = code.splitlines()
        return "\n".join([*lines[:insert_line], insertion, "", *lines[insert_line:]]) + "\n"

    lines = code.splitlines()
    insert_at = 0
    while (
        insert_at < len(lines)
        and (
            not lines[insert_at].strip()
            or lines[insert_at].lstrip().startswith("#")
            or lines[insert_at].startswith("from __future__ import ")
        )
    ):
        insert_at += 1
    return "\n".join([*lines[:insert_at], insertion, "", *lines[insert_at:]]) + "\n"
