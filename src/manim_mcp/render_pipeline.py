"""End-to-end render orchestration: code -> narration -> render -> mux -> quality."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_NARRATION_MODEL,
    DEFAULT_NARRATION_PROVIDER,
    HARD_NARRATION_CHAR_LIMIT,
    MAX_CODE_CHARS,
    RENDER_ROOT,
    SOFT_NARRATION_CHAR_LIMIT,
    NarrationAudioMode,
    NarrationSyncMode,
    Quality,
    OutputFormat,
    new_job_id,
    tail,
)
from .narration import (
    align_segmented_audio_to_timeline,
    build_narration_timing_plan,
    load_prepared_narration,
    mux_narration_audio,
    narration_tts_backend,
    public_audio_metadata,
    synthesize_narration_audio,
    synthesize_segmented_narration_audio,
)
from .quality import analyze_render_quality, load_narration_timeline_actual
from .render_io import (
    build_manim_command,
    create_preview_html,
    discover_artifacts,
    extract_render_error_summary,
    primary_video_artifact,
    render_env,
    render_scene_tool_result,
    update_access_metadata,
    update_final_response_metadata,
    validate_render_options,
    write_text,
)
from .safety import (
    analyze_all_constructs,
    analyze_animation_kwargs,
    analyze_code_safety,
    analyze_code_validation,
    infer_scene_name,
)
from .scene_prepare import prepare_narrated_scene_code


def add_narration_to_render(
    metadata: dict[str, Any],
    narration_text: str,
    *,
    model: str = DEFAULT_NARRATION_MODEL,
    provider: str = DEFAULT_NARRATION_PROVIDER,
    tts_timeout_seconds: int = 120,
    mux_timeout_seconds: int = 300,
    sync_mode: NarrationSyncMode = "timeline",
    audio: dict[str, Any] | None = None,
    audio_path: Path | None = None,
) -> dict[str, Any]:
    artifacts = metadata.get("artifacts") or []
    primary = primary_video_artifact(artifacts)
    if not primary:
        raise ValueError("Narration can only be added to video outputs.")
    if primary.get("format") != "mp4":
        raise ValueError("Narration currently requires output_format='mp4'.")

    job_dir = Path(metadata["job_dir"])
    narration_dir = job_dir / "narration"
    narration_dir.mkdir(parents=True, exist_ok=True)
    resolved_audio_path = audio_path or narration_dir / "narration.wav"
    original_video_path = Path(primary["path"])
    narrated_video_path = original_video_path.with_name(f"{original_video_path.stem}_narrated.mp4")

    if audio is None:
        audio = synthesize_narration_audio(
            narration_text,
            resolved_audio_path,
            model=model,
            provider=provider,
            timeout_seconds=tts_timeout_seconds,
        )
    if (
        sync_mode == "timeline"
        and audio.get("audio_mode") == "segmented"
        and metadata.get("narration_timeline_actual")
    ):
        audio = align_segmented_audio_to_timeline(
            audio,
            metadata["narration_timeline_actual"],
            narration_dir / "narration.timeline.wav",
            timeout_seconds=mux_timeout_seconds,
        )
    muxed_video = mux_narration_audio(
        original_video_path,
        Path(audio["path"]),
        narrated_video_path,
        sync_mode=sync_mode,
        timeout_seconds=mux_timeout_seconds,
    )
    public_roots = [job_dir]
    prepared = metadata.get("prepared_narration") or {}
    prepared_dir = prepared.get("prepared_narration_dir")
    if isinstance(prepared_dir, str):
        public_roots.append(Path(prepared_dir))
    public_audio = public_audio_metadata(audio, public_roots)
    metadata["narration_audio"] = public_audio
    metadata["narration"] = {
        "text": narration_text,
        "audio": public_audio,
        "video": muxed_video,
    }
    metadata["artifacts"] = discover_artifacts(Path(metadata["media_dir"]))
    return metadata


def render_scene_metadata(
    code: str,
    scene_name: str | None = None,
    quality: Quality = "low",
    output_format: OutputFormat = "mp4",
    save_last_frame: bool = False,
    timeout_seconds: int = 120,
    narration_text: str | None = None,
    prepared_narration_id: str | None = None,
    narration_model: str = DEFAULT_NARRATION_MODEL,
    narration_provider: str = DEFAULT_NARRATION_PROVIDER,
    narration_tts_timeout_seconds: int = 120,
    narration_mux_timeout_seconds: int = 300,
    narration_sync_mode: NarrationSyncMode = "timeline",
    narration_audio_mode: NarrationAudioMode = "segmented",
    visual_quality_checks: bool = True,
    fail_on_quality_issues: bool = False,
) -> dict[str, Any]:
    started = time.monotonic()
    prepared_narration: dict[str, Any] | None = None
    resolved_narration_text = narration_text.strip() if narration_text else None
    auto_upgrade_warning: str | None = None

    try:
        if not code.strip():
            raise ValueError("code must not be empty.")
        if len(code) > MAX_CODE_CHARS:
            raise ValueError(f"code exceeds the {MAX_CODE_CHARS} character limit.")
        validate_render_options(quality, output_format, timeout_seconds)
        if narration_sync_mode not in {"timeline", "fit", "pad"}:
            raise ValueError("narration_sync_mode must be one of: timeline, fit, pad.")
        if narration_audio_mode not in {"segmented", "single"}:
            raise ValueError("narration_audio_mode must be one of: segmented, single.")
        if narration_text and prepared_narration_id:
            raise ValueError("Use either narration_text or prepared_narration_id, not both.")

        if resolved_narration_text and len(resolved_narration_text) > HARD_NARRATION_CHAR_LIMIT:
            raise ValueError(
                f"narration_text is too long ({len(resolved_narration_text)} chars). "
                f"Keep it under {HARD_NARRATION_CHAR_LIMIT} characters; the tool targets <2-minute videos."
            )

        if prepared_narration_id:
            prepared_narration = load_prepared_narration(prepared_narration_id)
            resolved_narration_text = prepared_narration["narration_text"]
            narration_audio_mode = prepared_narration.get("narration_audio_mode", narration_audio_mode)
            narration_model = prepared_narration.get("narration_model", narration_model)
            narration_provider = prepared_narration.get("narration_provider", narration_provider)
        if resolved_narration_text and output_format != "mp4":
            raise ValueError("Narration currently requires output_format='mp4'.")

        # Auto-upgrade: timeline sync only delivers true sentence sync with measured
        # per-segment audio. If the caller asked for single-file audio, switch to
        # segmented and surface that we did so.
        if (
            resolved_narration_text
            and narration_sync_mode == "timeline"
            and narration_audio_mode == "single"
            and prepared_narration is None
        ):
            auto_upgrade_warning = (
                "narration_audio_mode auto-upgraded from 'single' to 'segmented' "
                "because narration_sync_mode='timeline' needs per-sentence durations."
            )
            narration_audio_mode = "segmented"

        violations = analyze_code_safety(code)
        if violations:
            return {
                "success": False,
                "blocked": True,
                "error": "Scene code failed safety preflight checks.",
                "violations": violations,
            }

        resolved_scene_name = infer_scene_name(code, scene_name)
        validation_violations = analyze_code_validation(code, resolved_scene_name)
        validation_violations.extend(analyze_all_constructs(code))
        if validation_violations:
            return {
                "success": False,
                "blocked": False,
                "error": "Scene code failed validation preflight checks.",
                "violations": validation_violations,
            }
    except Exception as exc:
        return {
            "success": False,
            "blocked": False,
            "error": str(exc),
        }

    job_id = new_job_id()
    job_dir = RENDER_ROOT / job_id
    media_dir = job_dir / "media"
    log_dir = job_dir / "logs"
    script_path = job_dir / "scene.py"
    stdout_path = job_dir / "render.stdout.log"
    stderr_path = job_dir / "render.stderr.log"
    metadata_path = job_dir / "metadata.json"

    job_dir.mkdir(parents=True, exist_ok=False)
    media_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    command = build_manim_command(
        script_path=script_path,
        scene_name=resolved_scene_name,
        media_dir=media_dir,
        log_dir=log_dir,
        quality=quality,
        output_format=output_format,
        save_last_frame=save_last_frame,
    )

    metadata: dict[str, Any] = {
        "success": False,
        "blocked": False,
        "job_id": job_id,
        "scene_name": resolved_scene_name,
        "quality": quality,
        "format": output_format,
        "save_last_frame": save_last_frame,
        "timeout_seconds": timeout_seconds,
        "narration_requested": bool(resolved_narration_text),
        "narration_text_chars": len(resolved_narration_text) if resolved_narration_text else 0,
        "prepared_narration_id": prepared_narration_id,
        "narration_model": narration_model if resolved_narration_text else None,
        "narration_provider": narration_provider if resolved_narration_text else None,
        "narration_tts_backend": narration_tts_backend(narration_provider) if resolved_narration_text else None,
        "narration_sync_mode": narration_sync_mode if resolved_narration_text else None,
        "narration_audio_mode": narration_audio_mode if resolved_narration_text else None,
        "job_dir": str(job_dir.resolve()),
        "script_path": str(script_path.resolve()),
        "media_dir": str(media_dir.resolve()),
        "log_dir": str(log_dir.resolve()),
        "stdout_log": str(stdout_path.resolve()),
        "stderr_log": str(stderr_path.resolve()),
        "command": command,
        "warnings": [],
    }
    if auto_upgrade_warning:
        metadata["warnings"].append({"code": "audio_mode_auto_upgrade", "message": auto_upgrade_warning})

    # Animation-kwarg sanity warnings (non-blocking; help LLM debug
    # "Animation.__init__() got an unexpected keyword argument" failures).
    kwarg_warnings = analyze_animation_kwargs(code)
    if kwarg_warnings:
        metadata["warnings"].append(
            {"code": "animation_kwarg_misuse", "messages": kwarg_warnings}
        )

    if (
        resolved_narration_text
        and len(resolved_narration_text) > SOFT_NARRATION_CHAR_LIMIT
    ):
        metadata["warnings"].append({
            "code": "narration_long",
            "message": (
                f"narration_text is {len(resolved_narration_text)} chars. "
                f"This tool targets sub-2-minute videos; aim for <{SOFT_NARRATION_CHAR_LIMIT} chars."
            ),
        })

    render_code = code
    pre_render_audio: dict[str, Any] | None = None
    pre_render_audio_path: Path | None = None
    if resolved_narration_text:
        try:
            narration_dir = job_dir / "narration"
            narration_dir.mkdir(parents=True, exist_ok=True)
            pre_render_audio_path = narration_dir / "narration.wav"
            public_audio_roots = [job_dir]
            if prepared_narration:
                pre_render_audio = prepared_narration["audio"]
                pre_render_audio_path = Path(pre_render_audio["path"])
                timing_plan = prepared_narration["timing_plan"]
                prepared_dir = Path(prepared_narration["prepared_narration_dir"])
                public_audio_roots.append(prepared_dir)
                metadata["prepared_narration"] = {
                    "prepared_narration_id": prepared_narration["prepared_narration_id"],
                    "prepared_narration_dir": prepared_narration["prepared_narration_dir"],
                    "timing_path": prepared_narration["timing_path"],
                }
            elif narration_audio_mode == "segmented":
                pre_render_audio, timing_plan = synthesize_segmented_narration_audio(
                    resolved_narration_text,
                    narration_dir,
                    model=narration_model,
                    provider=narration_provider,
                    timeout_seconds=narration_tts_timeout_seconds,
                    concat_timeout_seconds=narration_mux_timeout_seconds,
                )
                pre_render_audio_path = Path(pre_render_audio["path"])
            else:
                pre_render_audio = synthesize_narration_audio(
                    resolved_narration_text,
                    pre_render_audio_path,
                    model=narration_model,
                    provider=narration_provider,
                    timeout_seconds=narration_tts_timeout_seconds,
                )
                timing_plan = build_narration_timing_plan(
                    resolved_narration_text,
                    total_duration_seconds=pre_render_audio.get("duration_seconds"),
                )
            timing_path = narration_dir / "timing_plan.json"
            timing_path.write_text(json.dumps(timing_plan, indent=2) + "\n", encoding="utf-8")
            source_script_path = job_dir / "scene.original.py"
            source_script_path.write_text(code, encoding="utf-8")
            render_code, sync_report = prepare_narrated_scene_code(
                code,
                scene_name=resolved_scene_name,
                timing_plan=timing_plan,
                sync_mode=narration_sync_mode,
            )
            metadata.update({
                "source_script_path": str(source_script_path.resolve()),
                "narration_audio": public_audio_metadata(pre_render_audio, public_audio_roots),
                "narration_timing_plan": timing_plan,
                "narration_timing_path": str(timing_path.resolve()),
                "narration_sync": sync_report,
            })
        except Exception as exc:
            script_path.write_text(code, encoding="utf-8")
            write_text(stdout_path, "")
            write_text(stderr_path, f"{type(exc).__name__}: {exc}")
            metadata.update({
                "success": False,
                "duration_seconds": round(time.monotonic() - started, 3),
                "artifacts": discover_artifacts(media_dir),
                "stdout_tail": "",
                "stderr_tail": f"{type(exc).__name__}: {exc}",
                "error": f"Narration failed before render: {exc}",
            })
            metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
            return metadata

    script_path.write_text(render_code, encoding="utf-8")

    try:
        completed = subprocess.run(
            command,
            cwd=job_dir,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
            env=render_env(job_dir),
        )
        write_text(stdout_path, completed.stdout)
        write_text(stderr_path, completed.stderr)
        artifacts = discover_artifacts(media_dir)
        stderr_tail = tail(completed.stderr)
        metadata.update({
            "success": completed.returncode == 0 and bool(artifacts),
            "returncode": completed.returncode,
            "duration_seconds": round(time.monotonic() - started, 3),
            "artifacts": artifacts,
            "stdout_tail": tail(completed.stdout),
            "stderr_tail": stderr_tail,
        })
        if completed.returncode != 0:
            summary = extract_render_error_summary(stderr_tail)
            if summary:
                metadata["render_error_summary"] = summary
                metadata["error"] = f"Manim failed: {summary}"
            else:
                metadata["error"] = "Manim exited with a non-zero status."
        elif not artifacts:
            metadata["error"] = "Manim completed but no output artifact was discovered."
    except subprocess.TimeoutExpired as exc:
        write_text(stdout_path, exc.stdout)
        write_text(stderr_path, exc.stderr)
        metadata.update({
            "success": False,
            "timed_out": True,
            "duration_seconds": round(time.monotonic() - started, 3),
            "artifacts": discover_artifacts(media_dir),
            "stdout_tail": tail(exc.stdout),
            "stderr_tail": tail(exc.stderr),
            "error": f"Render timed out after {timeout_seconds} seconds.",
        })
    except Exception as exc:
        write_text(stdout_path, "")
        write_text(stderr_path, f"{type(exc).__name__}: {exc}")
        metadata.update({
            "success": False,
            "duration_seconds": round(time.monotonic() - started, 3),
            "artifacts": discover_artifacts(media_dir),
            "stdout_tail": "",
            "stderr_tail": f"{type(exc).__name__}: {exc}",
            "error": f"{type(exc).__name__}: {exc}",
        })

    if resolved_narration_text and metadata.get("narration_timing_plan"):
        timeline_actual = load_narration_timeline_actual(
            job_dir,
            metadata.get("narration_timing_plan"),
        )
        if timeline_actual:
            metadata["narration_timeline_actual"] = timeline_actual

    if metadata.get("success") and resolved_narration_text:
        try:
            add_narration_to_render(
                metadata,
                resolved_narration_text,
                model=narration_model,
                provider=narration_provider,
                tts_timeout_seconds=narration_tts_timeout_seconds,
                mux_timeout_seconds=narration_mux_timeout_seconds,
                sync_mode=narration_sync_mode,
                audio=pre_render_audio,
                audio_path=pre_render_audio_path,
            )
        except Exception as exc:
            metadata["success"] = False
            metadata["error"] = f"Narration failed: {exc}"

    if metadata.get("success"):
        create_preview_html(metadata)
        quality_report = analyze_render_quality(metadata, visual_checks=visual_quality_checks)
        if fail_on_quality_issues and not quality_report["ok"]:
            issue_messages = [
                issue["message"] for issue in quality_report["issues"] if issue["severity"] == "error"
            ]
            metadata["success"] = False
            metadata["quality_failed"] = True
            metadata["error"] = "Render completed, but quality checks failed: " + " ".join(issue_messages)

    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata


# Re-export the tool result builder so tools.py only needs to import from here.
__all__ = [
    "add_narration_to_render",
    "render_scene_metadata",
    "render_scene_tool_result",
    "update_access_metadata",
    "update_final_response_metadata",
]
