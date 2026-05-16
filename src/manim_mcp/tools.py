"""Thin MCP tool wrappers.

Tools delegate everything substantive to the pipeline modules. The
docstrings used as tool descriptions live in :mod:`prompts` so we can iterate
on the LLM-facing copy in one place.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ResourceLink, TextContent

from .app_ui import (
    MANIM_RENDER_ARTIFACT_MIME_TYPE,
    MANIM_RENDER_ARTIFACT_URI_TEMPLATE,
    MANIM_RENDER_APP_MIME_TYPE,
    MANIM_RENDER_APP_URI,
    manim_render_app_resource,
    manim_render_resource_meta,
    manim_render_tool_meta,
)
from .config import (
    DEFAULT_NARRATION_MODEL,
    DEFAULT_NARRATION_PROVIDER,
    JOB_ID_RE,
    RENDER_ROOT,
    NarrationAudioMode,
    NarrationSyncMode,
    OutputFormat,
    Quality,
)
from .narration import prepare_narration_metadata
from .prompts import (
    GET_RENDER_ACCESS_DOC,
    LIST_RENDERS_DOC,
    PREPARE_NARRATION_DOC,
    READ_RENDER_LOG_DOC,
    RENDER_SCENE_DOC,
    RENDER_SCENE_WITH_NARRATION_DOC,
    RENDER_SCENE_WITH_PREPARED_NARRATION_DOC,
    write_narrated_manim_scene_prompt_body,
)
from .render_io import (
    create_preview_html,
    discover_artifacts,
    latest_render_job_dir,
    primary_video_artifact,
    render_asset_url,
    render_scene_tool_result,
    update_access_metadata,
    update_final_response_metadata,
)
from .render_pipeline import render_scene_metadata


# ---------------------------------------------------------------------------
# 1. prepare_narration
# ---------------------------------------------------------------------------

def prepare_narration(
    narration_text: str,
    narration_model: str = DEFAULT_NARRATION_MODEL,
    narration_provider: str = DEFAULT_NARRATION_PROVIDER,
    narration_tts_timeout_seconds: int = 120,
    narration_mux_timeout_seconds: int = 300,
    narration_audio_mode: NarrationAudioMode = "segmented",
) -> dict[str, Any]:
    return prepare_narration_metadata(
        narration_text,
        narration_model=narration_model,
        narration_provider=narration_provider,
        narration_tts_timeout_seconds=narration_tts_timeout_seconds,
        narration_mux_timeout_seconds=narration_mux_timeout_seconds,
        narration_audio_mode=narration_audio_mode,
    )


prepare_narration.__doc__ = PREPARE_NARRATION_DOC


# ---------------------------------------------------------------------------
# 2. render_scene (silent or narrated)
# ---------------------------------------------------------------------------

def render_scene(
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
    include_resource_links: bool = True,
    include_ui_resource: bool = False,
    embed_preview_html: bool = False,
    embed_video_bytes: bool = False,
    max_inline_video_bytes: int = 2_000_000,
    max_inline_ui_video_bytes: int = 0,
) -> CallToolResult:
    metadata = render_scene_metadata(
        code=code,
        scene_name=scene_name,
        quality=quality,
        output_format=output_format,
        save_last_frame=save_last_frame,
        timeout_seconds=timeout_seconds,
        narration_text=narration_text,
        prepared_narration_id=prepared_narration_id,
        narration_model=narration_model,
        narration_provider=narration_provider,
        narration_tts_timeout_seconds=narration_tts_timeout_seconds,
        narration_mux_timeout_seconds=narration_mux_timeout_seconds,
        narration_sync_mode=narration_sync_mode,
        narration_audio_mode=narration_audio_mode,
        visual_quality_checks=visual_quality_checks,
        fail_on_quality_issues=fail_on_quality_issues,
    )
    return render_scene_tool_result(
        metadata,
        include_resource_links=include_resource_links,
        include_ui_resource=include_ui_resource,
        embed_preview_html=embed_preview_html,
        embed_video_bytes=embed_video_bytes,
        max_inline_video_bytes=max_inline_video_bytes,
        max_inline_ui_video_bytes=max_inline_ui_video_bytes,
    )


render_scene.__doc__ = RENDER_SCENE_DOC


# ---------------------------------------------------------------------------
# 3. render_scene_with_narration
# ---------------------------------------------------------------------------

def render_scene_with_narration(
    code: str,
    narration_text: str,
    scene_name: str | None = None,
    quality: Quality = "low",
    timeout_seconds: int = 120,
    narration_model: str = DEFAULT_NARRATION_MODEL,
    narration_provider: str = DEFAULT_NARRATION_PROVIDER,
    narration_tts_timeout_seconds: int = 120,
    narration_mux_timeout_seconds: int = 300,
    narration_sync_mode: NarrationSyncMode = "timeline",
    narration_audio_mode: NarrationAudioMode = "segmented",
    visual_quality_checks: bool = True,
    fail_on_quality_issues: bool = True,
    include_resource_links: bool = True,
    include_ui_resource: bool = False,
    embed_preview_html: bool = False,
    embed_video_bytes: bool = False,
    max_inline_video_bytes: int = 2_000_000,
    max_inline_ui_video_bytes: int = 0,
) -> CallToolResult:
    return render_scene(
        code=code,
        scene_name=scene_name,
        quality=quality,
        output_format="mp4",
        save_last_frame=False,
        timeout_seconds=timeout_seconds,
        narration_text=narration_text,
        narration_model=narration_model,
        narration_provider=narration_provider,
        narration_tts_timeout_seconds=narration_tts_timeout_seconds,
        narration_mux_timeout_seconds=narration_mux_timeout_seconds,
        narration_sync_mode=narration_sync_mode,
        narration_audio_mode=narration_audio_mode,
        visual_quality_checks=visual_quality_checks,
        fail_on_quality_issues=fail_on_quality_issues,
        include_resource_links=include_resource_links,
        include_ui_resource=include_ui_resource,
        embed_preview_html=embed_preview_html,
        embed_video_bytes=embed_video_bytes,
        max_inline_video_bytes=max_inline_video_bytes,
        max_inline_ui_video_bytes=max_inline_ui_video_bytes,
    )


render_scene_with_narration.__doc__ = RENDER_SCENE_WITH_NARRATION_DOC


# ---------------------------------------------------------------------------
# 4. render_scene_with_prepared_narration
# ---------------------------------------------------------------------------

def render_scene_with_prepared_narration(
    code: str,
    prepared_narration_id: str,
    scene_name: str | None = None,
    quality: Quality = "low",
    timeout_seconds: int = 120,
    narration_mux_timeout_seconds: int = 300,
    narration_sync_mode: NarrationSyncMode = "timeline",
    visual_quality_checks: bool = True,
    fail_on_quality_issues: bool = True,
    include_resource_links: bool = True,
    include_ui_resource: bool = False,
    embed_preview_html: bool = False,
    embed_video_bytes: bool = False,
    max_inline_video_bytes: int = 2_000_000,
    max_inline_ui_video_bytes: int = 0,
) -> CallToolResult:
    return render_scene(
        code=code,
        scene_name=scene_name,
        quality=quality,
        output_format="mp4",
        save_last_frame=False,
        timeout_seconds=timeout_seconds,
        narration_text=None,
        prepared_narration_id=prepared_narration_id,
        narration_mux_timeout_seconds=narration_mux_timeout_seconds,
        narration_sync_mode=narration_sync_mode,
        narration_audio_mode="segmented",
        visual_quality_checks=visual_quality_checks,
        fail_on_quality_issues=fail_on_quality_issues,
        include_resource_links=include_resource_links,
        include_ui_resource=include_ui_resource,
        embed_preview_html=embed_preview_html,
        embed_video_bytes=embed_video_bytes,
        max_inline_video_bytes=max_inline_video_bytes,
        max_inline_ui_video_bytes=max_inline_ui_video_bytes,
    )


render_scene_with_prepared_narration.__doc__ = RENDER_SCENE_WITH_PREPARED_NARRATION_DOC


# ---------------------------------------------------------------------------
# 5. get_render_access (recover links for an existing job)
# ---------------------------------------------------------------------------

def _job_dir_for(job_id: str) -> Path:
    if not JOB_ID_RE.match(job_id):
        raise ValueError("Invalid job_id format.")
    job_dir = (RENDER_ROOT / job_id).resolve()
    render_root = RENDER_ROOT.resolve()
    if render_root not in job_dir.parents and job_dir != render_root:
        raise ValueError("Invalid job_id path.")
    return job_dir


def render_artifact_resource(job_id: str) -> bytes:
    """Return the primary render artifact bytes for MCP Apps inline playback."""
    job_dir = _job_dir_for(job_id)
    metadata_path = job_dir / "metadata.json"
    if not metadata_path.exists():
        raise ValueError(f"Render job '{job_id}' has no metadata.json.")

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"metadata.json is invalid JSON: {exc}") from exc

    artifacts = metadata.get("artifacts") or []
    if not artifacts and (job_dir / "media").exists():
        artifacts = discover_artifacts(job_dir / "media")

    primary = primary_video_artifact(artifacts) or (artifacts[0] if artifacts else None)
    if not primary:
        raise ValueError(f"No render artifact was found for job '{job_id}'.")

    artifact_path = Path(primary["path"]).resolve()
    render_root = RENDER_ROOT.resolve()
    if artifact_path != render_root and render_root not in artifact_path.parents:
        raise ValueError("Render artifact is outside render root.")
    if not artifact_path.is_file():
        raise ValueError(f"Render artifact no longer exists: {artifact_path}")
    return artifact_path.read_bytes()


def get_render_access(job_id: str = "latest") -> CallToolResult:
    try:
        if job_id == "latest":
            job_dir = latest_render_job_dir()
            if job_dir is None:
                raise ValueError("No render jobs were found.")
        else:
            job_dir = _job_dir_for(job_id)
    except ValueError as exc:
        metadata = {"success": False, "error": str(exc), "job_id": job_id}
        return CallToolResult(
            content=[TextContent(type="text", text=str(exc))],
            structuredContent=metadata,
            isError=True,
        )

    metadata_path = job_dir / "metadata.json"
    if not metadata_path.exists():
        metadata = {"success": False, "error": f"Render job '{job_dir.name}' has no metadata.json."}
        return CallToolResult(
            content=[TextContent(type="text", text=metadata["error"])],
            structuredContent=metadata,
            isError=True,
        )

    try:
        full_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        metadata = {"success": False, "error": f"metadata.json is invalid JSON: {exc}", "job_id": job_dir.name}
        return CallToolResult(
            content=[TextContent(type="text", text=metadata["error"])],
            structuredContent=metadata,
            isError=True,
        )

    artifacts = full_metadata.get("artifacts") or []
    if not artifacts and (job_dir / "media").exists():
        artifacts = discover_artifacts(job_dir / "media")
        full_metadata["artifacts"] = artifacts
    primary = primary_video_artifact(artifacts) or (artifacts[0] if artifacts else None)
    preview = full_metadata.get("preview_html")
    if primary and not preview:
        preview = create_preview_html(full_metadata)

    access = update_access_metadata(full_metadata, primary, preview)
    final_response = update_final_response_metadata(
        full_metadata,
        access,
        status_text=f"Your Manim render `{full_metadata.get('scene_name', job_dir.name)}` is ready.",
    )
    compact_metadata = {
        "success": bool(primary),
        "job_id": full_metadata.get("job_id", job_dir.name),
        "scene_name": full_metadata.get("scene_name"),
        "access": access,
        "final_response_markdown": final_response,
        "claude_response_instructions": full_metadata.get(
            "claude_response_instructions",
            "Include final_response_markdown verbatim in the next assistant message.",
        ),
    }
    if not primary:
        compact_metadata["error"] = "No render artifact was found for this job."

    content: list[TextContent | ResourceLink] = [
        TextContent(
            type="text",
            text="MANIM RENDER ACCESS - include this block in your reply to the user:\n" + final_response,
        )
    ]
    if primary and access.get("video_stream_url"):
        content.append(
            ResourceLink(
                type="resource_link",
                name=Path(primary["path"]).name,
                title=f"{full_metadata.get('scene_name')} app video stream",
                uri=access["video_stream_url"],
                description="Localhost video stream for opening inside MCP clients.",
                mimeType=primary.get("mime_type", "application/octet-stream"),
                size=primary.get("size_bytes"),
            )
        )
    if preview and access.get("preview_stream_url"):
        content.append(
            ResourceLink(
                type="resource_link",
                name="player.html",
                title=f"{full_metadata.get('scene_name')} app video player",
                uri=access["preview_stream_url"],
                description="Localhost HTML player for opening inside MCP clients.",
                mimeType="text/html",
                size=preview.get("size_bytes"),
            )
        )
    return CallToolResult(content=content, structuredContent=compact_metadata, isError=not bool(primary))


get_render_access.__doc__ = GET_RENDER_ACCESS_DOC


# ---------------------------------------------------------------------------
# 6. list_renders
# ---------------------------------------------------------------------------

def list_renders(limit: int = 20) -> dict[str, Any]:
    safe_limit = max(1, min(limit, 100))
    if not RENDER_ROOT.exists():
        return {"render_root": str(RENDER_ROOT.resolve()), "renders": []}

    jobs: list[dict[str, Any]] = []
    for job_dir in sorted(RENDER_ROOT.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if not job_dir.is_dir():
            continue
        metadata_path = job_dir / "metadata.json"
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                metadata = {"job_id": job_dir.name, "error": "metadata.json is invalid JSON"}
        else:
            metadata = {"job_id": job_dir.name, "job_dir": str(job_dir.resolve())}
        jobs.append(metadata)
        if len(jobs) >= safe_limit:
            break
    return {"render_root": str(RENDER_ROOT.resolve()), "renders": jobs}


list_renders.__doc__ = LIST_RENDERS_DOC


# ---------------------------------------------------------------------------
# 7. read_render_log
# ---------------------------------------------------------------------------

def read_render_log(job_id: str, max_chars: int = 8000) -> dict[str, Any]:
    try:
        job_dir = _job_dir_for(job_id)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    if not job_dir.exists():
        return {"success": False, "error": f"Render job '{job_id}' was not found."}

    safe_max_chars = max(100, min(max_chars, 100_000))
    stdout_path = job_dir / "render.stdout.log"
    stderr_path = job_dir / "render.stderr.log"
    stdout = stdout_path.read_text(encoding="utf-8") if stdout_path.exists() else ""
    stderr = stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else ""

    def _bounded(text: str) -> str:
        if not text:
            return ""
        return text[-safe_max_chars:]

    return {
        "success": True,
        "job_id": job_id,
        "stdout_log": str(stdout_path.resolve()),
        "stderr_log": str(stderr_path.resolve()),
        "stdout_tail": _bounded(stdout),
        "stderr_tail": _bounded(stderr),
    }


read_render_log.__doc__ = READ_RENDER_LOG_DOC


# ---------------------------------------------------------------------------
# 8. Prompt template
# ---------------------------------------------------------------------------

def write_narrated_manim_scene_prompt(topic: str, quality: str = "low") -> str:
    """Prompt template for synchronized narrated Manim scenes."""
    return write_narrated_manim_scene_prompt_body(topic, quality)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_tools(mcp: FastMCP) -> None:
    """Attach every tool + prompt to the given FastMCP instance."""
    mcp.resource(
        MANIM_RENDER_APP_URI,
        name="manim_render_player",
        title="Manim Render Player",
        description="Inline video player for Manim render results.",
        mime_type=MANIM_RENDER_APP_MIME_TYPE,
        meta=manim_render_resource_meta(),
    )(manim_render_app_resource)
    mcp.resource(
        MANIM_RENDER_ARTIFACT_URI_TEMPLATE,
        name="manim_render_artifact",
        title="Manim Render Artifact",
        description="Primary render artifact bytes for MCP Apps inline playback.",
        mime_type=MANIM_RENDER_ARTIFACT_MIME_TYPE,
    )(render_artifact_resource)

    mcp.tool()(prepare_narration)
    mcp.tool(meta=manim_render_tool_meta())(render_scene)
    mcp.tool(meta=manim_render_tool_meta())(render_scene_with_narration)
    mcp.tool(meta=manim_render_tool_meta())(render_scene_with_prepared_narration)
    mcp.tool(meta=manim_render_tool_meta())(get_render_access)
    mcp.tool()(list_renders)
    mcp.tool()(read_render_log)

    mcp.prompt(
        name="write_narrated_manim_scene",
        title="Write Narrated Manim Scene",
        description=(
            "Create a ManimCE scene that is synchronized to narration, stays "
            "inside frame, and matches one visual beat to one spoken sentence."
        ),
    )(write_narrated_manim_scene_prompt)
