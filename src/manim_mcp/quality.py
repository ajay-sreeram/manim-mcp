"""Post-render quality checks.

Three layers:

* :func:`analyze_video_frame_bounds` -- samples frames and looks for content
  touching the visible frame edge, plus a *soft* visual-density signal.
* :func:`load_narration_timeline_actual` -- reads the incremental
  ``narration/timeline_actual.json`` written by the injected helper.
* :func:`analyze_render_quality` -- combines static analysis (from
  ``prepare_narrated_scene_code``), measured render timeline, and frame
  samples into a single ordered ``issues`` list that's safe to surface to
  the LLM.

We deliberately downgrade the noisier heuristics (visual clutter density,
mobject count) to *warnings* so a clean dense diagram never blocks render.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import (
    MAX_VIDEO_EXTEND_SECONDS,
    SEGMENT_AUDIO_OVERFLOW_TOLERANCE,
)
from .render_io import (
    extract_raw_frame,
    primary_video_artifact,
    probe_media_duration,
    video_stream_dimensions,
)


# ---------------------------------------------------------------------------
# 1. Background colour detection (border-ring majority vote)
# ---------------------------------------------------------------------------

def _border_pixel_indices(width: int, height: int, ring_thickness: int = 6) -> list[int]:
    indices: list[int] = []
    ring = max(1, min(ring_thickness, width // 4, height // 4))
    for y in range(height):
        if y < ring or y >= height - ring:
            for x in range(0, width, 2):
                indices.append((y * width + x) * 3)
        else:
            for x in range(0, ring, 2):
                indices.append((y * width + x) * 3)
            for x in range(width - ring, width, 2):
                indices.append((y * width + x) * 3)
    return indices


def _detect_background(frame: bytes, width: int, height: int) -> tuple[tuple[int, int, int], float]:
    """Return ((r,g,b), confidence in [0,1])."""
    indices = _border_pixel_indices(width, height)
    if not indices:
        return ((0, 0, 0), 0.0)

    r_values = [frame[i] for i in indices]
    g_values = [frame[i + 1] for i in indices]
    b_values = [frame[i + 2] for i in indices]
    background = (
        sorted(r_values)[len(r_values) // 2],
        sorted(g_values)[len(g_values) // 2],
        sorted(b_values)[len(b_values) // 2],
    )

    # Confidence = fraction of border pixels within 30 of the median per channel.
    matches = 0
    for index in indices:
        if (
            abs(frame[index] - background[0]) <= 30
            and abs(frame[index + 1] - background[1]) <= 30
            and abs(frame[index + 2] - background[2]) <= 30
        ):
            matches += 1
    confidence = matches / max(1, len(indices))
    return background, confidence


def _frame_content_bounds(
    frame: bytes,
    width: int,
    height: int,
    *,
    threshold: int = 35,
    step: int = 2,
) -> dict[str, Any] | None:
    background, confidence = _detect_background(frame, width, height)
    if confidence < 0.55:
        # Background couldn't be confidently identified; skip this frame.
        return {"low_confidence_background": True, "confidence": round(confidence, 3)}

    min_x, min_y = width, height
    max_x, max_y = -1, -1
    content_pixels = 0
    for y in range(0, height, step):
        row = y * width * 3
        for x in range(0, width, step):
            index = row + x * 3
            distance = (
                abs(frame[index] - background[0])
                + abs(frame[index + 1] - background[1])
                + abs(frame[index + 2] - background[2])
            )
            if distance <= threshold:
                continue
            content_pixels += 1
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x)
            max_y = max(max_y, y)

    if content_pixels < 20:
        return None
    total_sampled_pixels = ((width + step - 1) // step) * ((height + step - 1) // step)
    bbox_width = max_x - min_x + 1
    bbox_height = max_y - min_y + 1
    sampled_bbox_pixels = max(1, ((bbox_width + step - 1) // step) * ((bbox_height + step - 1) // step))
    return {
        "background_rgb": background,
        "background_confidence": round(confidence, 3),
        "bbox": {
            "x_min": min_x, "y_min": min_y,
            "x_max": max_x, "y_max": max_y,
            "width": bbox_width, "height": bbox_height,
        },
        "sampled_content_pixels": content_pixels,
        "content_pixel_ratio": round(content_pixels / max(total_sampled_pixels, 1), 4),
        "bbox_fill_ratio": round(content_pixels / sampled_bbox_pixels, 4),
    }


# ---------------------------------------------------------------------------
# 2. Sampled-frame edge + density analysis
# ---------------------------------------------------------------------------

def analyze_video_frame_bounds(
    video_path: Path,
    *,
    sample_count: int = 8,
    edge_margin_px: int = 12,
) -> dict[str, Any]:
    duration = probe_media_duration(video_path)
    dimensions = video_stream_dimensions(video_path)
    if duration is None or not dimensions:
        return {
            "ok": True,
            "warning": "Could not measure video dimensions or duration for frame-bound checks.",
            "samples": [],
        }

    width, height = dimensions
    samples: list[dict[str, Any]] = []
    edge_hits = 0
    confident_samples = 0
    times = [
        duration * (index + 1) / (sample_count + 1)
        for index in range(sample_count)
        if duration > 0
    ]
    for timestamp in times:
        frame = extract_raw_frame(video_path, timestamp, width, height)
        if frame is None:
            continue
        bounds = _frame_content_bounds(frame, width, height)
        if bounds is None:
            samples.append({"timestamp_seconds": round(timestamp, 3), "content_detected": False})
            continue
        if bounds.get("low_confidence_background"):
            samples.append({
                "timestamp_seconds": round(timestamp, 3),
                "content_detected": False,
                "low_confidence_background": True,
            })
            continue
        confident_samples += 1
        bbox = bounds["bbox"]
        touches_edge = (
            bbox["x_min"] <= edge_margin_px
            or bbox["y_min"] <= edge_margin_px
            or bbox["x_max"] >= width - edge_margin_px - 1
            or bbox["y_max"] >= height - edge_margin_px - 1
        )
        edge_hits += int(touches_edge)
        samples.append({
            "timestamp_seconds": round(timestamp, 3),
            "content_detected": True,
            "touches_edge": touches_edge,
            **bounds,
        })

    # Only flag edge touches if we had at least 3 high-confidence samples.
    edge_actionable = confident_samples >= 3
    edge_hit_ratio = edge_hits / confident_samples if confident_samples else 0.0
    content_ratios = [
        float(sample.get("content_pixel_ratio") or 0.0)
        for sample in samples
        if sample.get("content_detected")
    ]
    return {
        "ok": (not edge_actionable) or edge_hits == 0,
        "width": width,
        "height": height,
        "duration_seconds": duration,
        "sample_count": len(samples),
        "content_sample_count": confident_samples,
        "edge_touch_count": edge_hits,
        "edge_actionable": edge_actionable,
        "edge_hit_ratio": round(edge_hit_ratio, 3),
        "max_content_pixel_ratio": round(max(content_ratios), 3) if content_ratios else 0.0,
        "avg_content_pixel_ratio": round(sum(content_ratios) / len(content_ratios), 3)
        if content_ratios
        else 0.0,
        "edge_margin_px": edge_margin_px,
        "samples": samples,
    }


# ---------------------------------------------------------------------------
# 3. Reading the helper's incremental timeline_actual.json
# ---------------------------------------------------------------------------

def load_narration_timeline_actual(
    job_dir: Path,
    timing_plan: dict[str, Any] | None,
) -> dict[str, Any] | None:
    path = job_dir / "narration" / "timeline_actual.json"
    if not path.exists():
        return None
    try:
        actual = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    actual["path"] = str(path.resolve())
    actual["uri"] = path.resolve().as_uri()
    segments = (timing_plan or {}).get("segments") or []
    by_index = {
        int(segment["index"]): segment
        for segment in segments
        if isinstance(segment, dict) and isinstance(segment.get("index"), int)
    }
    enriched_events: list[dict[str, Any]] = []
    max_abs_start_delta = 0.0
    max_abs_end_delta = 0.0
    max_top_count = 0
    max_family_count = 0
    worst_start_event: dict[str, Any] | None = None
    worst_end_event: dict[str, Any] | None = None
    worst_clutter_event: dict[str, Any] | None = None

    for event in actual.get("timeline_events") or []:
        enriched = dict(event)
        index = event.get("segment_index")
        segment = by_index.get(index) if isinstance(index, int) else None
        if segment:
            planned_start = float(segment.get("start_seconds") or 0.0)
            planned_end = float(segment.get("end_seconds") or planned_start)
            actual_start = float(event.get("start_seconds") or 0.0)
            actual_end = float(event.get("end_seconds") or actual_start)
            start_delta = round(actual_start - planned_start, 3)
            end_delta = round(actual_end - planned_end, 3)
            duration_delta = round(
                float(event.get("actual_seconds") or 0.0)
                - float(segment.get("duration_seconds") or 0.0),
                3,
            )
            enriched.update({
                "planned_start_seconds": round(planned_start, 3),
                "planned_end_seconds": round(planned_end, 3),
                "start_delta_seconds": start_delta,
                "end_delta_seconds": end_delta,
                "duration_delta_seconds": duration_delta,
            })
            if abs(start_delta) > max_abs_start_delta:
                max_abs_start_delta = abs(start_delta)
                worst_start_event = enriched
            if abs(end_delta) > max_abs_end_delta:
                max_abs_end_delta = abs(end_delta)
                worst_end_event = enriched
        top_count = enriched.get("end_top_mobject_count")
        family_count = enriched.get("end_mobject_family_count")
        if isinstance(top_count, int | float) and int(top_count) > max_top_count:
            max_top_count = int(top_count)
            worst_clutter_event = enriched
        if isinstance(family_count, int | float):
            max_family_count = max(max_family_count, int(family_count))
        enriched_events.append(enriched)

    actual["timeline_events"] = enriched_events
    actual["sync_summary"] = {
        "max_abs_start_delta_seconds": round(max_abs_start_delta, 3),
        "max_abs_end_delta_seconds": round(max_abs_end_delta, 3),
        "max_end_top_mobject_count": max_top_count,
        "max_end_mobject_family_count": max_family_count,
        "worst_start_event": worst_start_event,
        "worst_end_event": worst_end_event,
        "worst_clutter_event": worst_clutter_event,
    }
    return actual


# ---------------------------------------------------------------------------
# 4. Issue ranking
# ---------------------------------------------------------------------------

_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}


def _sorted_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        issues,
        key=lambda item: (
            _SEVERITY_RANK.get(str(item.get("severity")), 9),
            int(item.get("segment_index") or 9999),
            str(item.get("code", "")),
        ),
    )


# ---------------------------------------------------------------------------
# 5. analyze_render_quality (the one called from the pipeline)
# ---------------------------------------------------------------------------

def analyze_render_quality(metadata: dict[str, Any], *, visual_checks: bool = True) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []

    narration_video = (metadata.get("narration") or {}).get("video") or {}
    narration_audio = (metadata.get("narration") or {}).get("audio") or {}
    narration_sync = metadata.get("narration_sync") or {}
    timeline_actual = metadata.get("narration_timeline_actual") or {}

    if metadata.get("narration_requested"):
        dynamic_loop_calls = int(narration_sync.get("dynamic_loop_timed_call_count") or 0)
        explicit_timeline = bool(narration_sync.get("explicit_timeline_used"))

        if dynamic_loop_calls and not explicit_timeline:
            issues.append({
                "severity": "error",
                "code": "dynamic_loop_timing",
                "message": (
                    f"{dynamic_loop_calls} timed calls are inside loops with unknown iteration counts, "
                    "so they could not be aligned to narration segments. Use narration_timeline(self) "
                    "with tl.play_segment(...) / tl.wait_segment(...)."
                ),
            })

        # Helper-emitted runtime warnings (e.g. play_segment hold clamped).
        for warning in timeline_actual.get("timeline_warnings") or []:
            issues.append({
                "severity": "warning",
                "code": "runtime_timeline_warning",
                "message": warning.get("message") or "narration_timeline runtime warning",
                "segment_index": warning.get("segment_index"),
                **{k: v for k, v in warning.items() if k != "message"},
            })

        # Audio overflow warnings (audio sentence outran its visual beat).
        for warning in narration_audio.get("audio_overflow_warnings") or []:
            issues.append({
                "severity": "warning",
                "code": "audio_overflow_segment",
                "message": (
                    f"Sentence {warning.get('segment_index')} audio outran its rendered visual "
                    f"beat by {warning.get('audio_overrun_seconds')}s "
                    f"(tolerance {SEGMENT_AUDIO_OVERFLOW_TOLERANCE}s). Either lengthen the matching "
                    "visual beat or split the narration sentence."
                ),
                "segment_index": warning.get("segment_index"),
                "audio_overrun_seconds": warning.get("audio_overrun_seconds"),
                "audio_duration_seconds": warning.get("audio_duration_seconds"),
            })

        if explicit_timeline:
            segment_count = int(narration_sync.get("explicit_timeline_segment_count") or 0)
            covered_count = int(narration_sync.get("explicit_timeline_covered_segment_count") or 0)
            missing_segments = narration_sync.get("explicit_timeline_missing_segments") or []
            out_of_range_segments = narration_sync.get("explicit_timeline_out_of_range_segments") or []
            outside_timeline_calls = int(
                timeline_actual.get("outside_timed_event_count")
                if timeline_actual.get("outside_timed_event_count") is not None
                else narration_sync.get("outside_timeline_timed_call_count")
                or 0
            )
            outside_timeline_seconds = float(
                timeline_actual.get("outside_timed_total_seconds")
                if timeline_actual.get("outside_timed_total_seconds") is not None
                else narration_sync.get("outside_timeline_estimated_seconds")
                or 0.0
            )
            dynamic_segment_calls = int(
                narration_sync.get("explicit_timeline_dynamic_segment_call_count") or 0
            )
            audio_timeline_aligned = bool(narration_audio.get("timeline_aligned"))

            if outside_timeline_calls:
                severity = (
                    "warning"
                    if audio_timeline_aligned
                    else "error"
                    if outside_timeline_seconds > 1.5 or outside_timeline_calls > 2
                    else "warning"
                )
                issues.append({
                    "severity": severity,
                    "code": "outside_timeline_timed_calls",
                    "message": (
                        f"{outside_timeline_calls} timed self.play/self.wait calls "
                        f"({outside_timeline_seconds:.2f}s measured) ran outside "
                        "tl.play_segment(...) / tl.wait_segment(...). Timeline audio "
                        "can insert silence for short transitions, but important visual "
                        "motion should live inside the narration segment that explains it."
                    ),
                    "outside_timeline_timed_call_count": outside_timeline_calls,
                    "outside_timeline_estimated_seconds": outside_timeline_seconds,
                    "measured_from_render": bool(timeline_actual),
                    "timeline_audio_aligned": audio_timeline_aligned,
                })

            sync_summary = timeline_actual.get("sync_summary") or {}
            max_start_delta = float(sync_summary.get("max_abs_start_delta_seconds") or 0.0)
            max_top_count = int(sync_summary.get("max_end_top_mobject_count") or 0)

            # Use top-level mobject count so glyph-rich text scenes don't trip the alarm.
            if max_top_count > 18:
                worst_event = sync_summary.get("worst_clutter_event") or {}
                issues.append({
                    "severity": "warning",
                    "code": "timeline_scene_mobject_clutter",
                    "message": (
                        f"A timeline beat ended with about {max_top_count} top-level mobjects on screen. "
                        "Earlier groups may not have been faded out before the next beat. "
                        "Use FadeOut(group) or self.clear() between subtopics."
                    ),
                    "max_end_top_mobject_count": max_top_count,
                    "segment_index": worst_event.get("segment_index"),
                })
            if max_start_delta > 0.75:
                worst_event = sync_summary.get("worst_start_event") or {}
                issues.append({
                    "severity": "warning" if audio_timeline_aligned else "error",
                    "code": "actual_timeline_start_drift",
                    "message": (
                        f"Measured render timing drifted by up to {max_start_delta:.2f}s "
                        "from the initial narration plan. Timeline-aligned audio can follow "
                        "the rendered starts, but large gaps may feel slow unless intended."
                    ),
                    "max_abs_start_delta_seconds": round(max_start_delta, 3),
                    "segment_index": worst_event.get("segment_index"),
                    "start_delta_seconds": worst_event.get("start_delta_seconds"),
                    "timeline_audio_aligned": audio_timeline_aligned,
                })
            if segment_count and covered_count < segment_count and not dynamic_segment_calls:
                issues.append({
                    "severity": "error",
                    "code": "incomplete_timeline_coverage",
                    "message": (
                        f"Only {covered_count} of {segment_count} narration segments are bound "
                        "with tl.play_segment(...) or tl.wait_segment(...). Unbound segments "
                        "leave the final frame idle while audio continues."
                    ),
                    "covered_segment_count": covered_count,
                    "segment_count": segment_count,
                    "missing_segments": missing_segments,
                })

            # Even when static analysis can't tell, cross-check what actually
            # happened at render time: did every segment_index appear?
            if segment_count and timeline_actual:
                rendered_indices = {
                    int(event.get("segment_index"))
                    for event in timeline_actual.get("timeline_events") or []
                    if isinstance(event.get("segment_index"), int) and not event.get("out_of_range")
                }
                rendered_missing = sorted(
                    i for i in range(segment_count) if i not in rendered_indices
                )
                if rendered_missing and "incomplete_timeline_coverage" not in {issue["code"] for issue in issues}:
                    issues.append({
                        "severity": "error",
                        "code": "rendered_timeline_missing_segments",
                        "message": (
                            f"The rendered timeline never executed segments {rendered_missing}. "
                            "Add a tl.play_segment(...) or tl.wait_segment(...) for each one, "
                            "even if it is a brief pause."
                        ),
                        "missing_segments": rendered_missing,
                    })

            if out_of_range_segments:
                issues.append({
                    "severity": "warning",
                    "code": "out_of_range_timeline_segment",
                    "message": (
                        "Some timeline calls use segment indices outside the narration range. "
                        "Bind narration only to indices 0 through segment_count - 1."
                    ),
                    "segment_count": segment_count,
                    "out_of_range_segments": out_of_range_segments,
                })

            alignment = narration_sync.get("timeline_alignment") or {}
            for alignment_issue in alignment.get("issues") or []:
                severity = alignment_issue.get("severity")
                if severity not in {"error", "warning"}:
                    severity = "warning"
                issues.append({
                    "severity": severity,
                    "code": alignment_issue.get("code", "timeline_visual_alignment"),
                    "message": alignment_issue.get(
                        "message",
                        "A timeline visual beat may not match its narration sentence.",
                    ),
                    "segment_index": alignment_issue.get("segment_index"),
                    "line": alignment_issue.get("line"),
                    "visual_terms": alignment_issue.get("visual_terms"),
                    "current_overlap_terms": alignment_issue.get("current_overlap_terms"),
                    "next_overlap_terms": alignment_issue.get("next_overlap_terms"),
                    "previous_overlap_terms": alignment_issue.get("previous_overlap_terms"),
                })

        # Mux-level issues.
        audio_duration = narration_video.get("audio_duration_seconds")
        original_video_duration = narration_video.get("video_duration_seconds")
        mode = narration_video.get("mode")
        fit_clamped = bool(narration_video.get("fit_clamped"))
        extend_capped = bool(narration_video.get("extend_capped"))

        if extend_capped:
            issues.append({
                "severity": "error",
                "code": "video_extend_capped",
                "message": (
                    f"The narration was much longer than the rendered video, so the final "
                    f"frame would have to be cloned for over {MAX_VIDEO_EXTEND_SECONDS:.0f}s. "
                    "Either shorten the narration, or extend the visual beats with "
                    "tl.wait_segment(...) so they fill the segment durations."
                ),
            })
        if fit_clamped and explicit_timeline:
            issues.append({
                "severity": "error",
                "code": "fit_retime_clamped",
                "message": (
                    "Global ffmpeg retime would have stretched the video beyond the safe range. "
                    "Use narration_sync_mode='timeline' with tl.play_segment(...) so the visuals "
                    "match the audio without speed changes."
                ),
            })

        if (
            mode == "fit_video_to_audio"
            and isinstance(audio_duration, (int, float))
            and isinstance(original_video_duration, (int, float))
            and audio_duration > 0
        ):
            delta = float(audio_duration) - float(original_video_duration)
            ratio = abs(delta) / max(float(audio_duration), 0.1)
            if ratio > 0.15 and explicit_timeline:
                issues.append({
                    "severity": "error",
                    "code": "severe_timeline_duration_mismatch",
                    "message": (
                        f"The timeline-rendered video duration ({original_video_duration:.2f}s) differed from "
                        f"narration ({audio_duration:.2f}s) by {abs(delta):.2f}s. The scene likely shadowed "
                        "the injected narration_timeline helper or has substantial timed calls outside it."
                    ),
                    "delta_seconds": round(delta, 3),
                    "ratio": round(ratio, 3),
                })
            elif ratio > 0.15:
                issues.append({
                    "severity": "error",
                    "code": "severe_global_retime",
                    "message": (
                        f"The silent video duration ({original_video_duration:.2f}s) differed from "
                        f"narration ({audio_duration:.2f}s) by {abs(delta):.2f}s, so ffmpeg globally "
                        f"retimed the entire video by {ratio:.0%}. This usually causes poor sync; "
                        "use timeline mode with explicit tl.play_segment(...) for tight pacing."
                    ),
                    "delta_seconds": round(delta, 3),
                    "ratio": round(ratio, 3),
                })

    primary = primary_video_artifact(metadata.get("artifacts") or [])
    if visual_checks and primary:
        bounds = analyze_video_frame_bounds(Path(primary["path"]))
        if not bounds.get("ok") and bounds.get("edge_touch_count", 0):
            issues.append({
                "severity": "warning",
                "code": "content_touches_frame_edge",
                "message": (
                    f"Rendered content touches the frame edge in {bounds.get('edge_touch_count')} "
                    f"of {bounds.get('content_sample_count')} sampled frames. Scale groups with "
                    "fit_to_safe_frame(...) or keep layout radii inside the visible frame."
                ),
                "edge_touch_count": bounds.get("edge_touch_count"),
                "content_sample_count": bounds.get("content_sample_count"),
                "edge_hit_ratio": bounds.get("edge_hit_ratio"),
            })
        # Density signal is informational only; many legit scenes look "dense".
        max_content_ratio = float(bounds.get("max_content_pixel_ratio") or 0.0)
        avg_content_ratio = float(bounds.get("avg_content_pixel_ratio") or 0.0)
        if bounds.get("content_sample_count", 0) >= 4 and (
            max_content_ratio > 0.30 or avg_content_ratio > 0.22
        ):
            issues.append({
                "severity": "warning",
                "code": "visual_density_high",
                "message": (
                    "Sampled frames look visually dense, which sometimes means too many "
                    "labels or earlier-scene objects are stacked. If intentional, ignore. "
                    "Otherwise fade or clear earlier groups before adding more."
                ),
                "max_content_pixel_ratio": round(max_content_ratio, 3),
                "avg_content_pixel_ratio": round(avg_content_ratio, 3),
                "content_sample_count": bounds.get("content_sample_count"),
            })
        metadata["visual_bounds"] = bounds

    issues = _sorted_issues(issues)
    ok = not any(issue["severity"] == "error" for issue in issues)
    quality = {"ok": ok, "issues": issues}
    metadata["quality_checks"] = quality
    return quality
