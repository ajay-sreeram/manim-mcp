"""Low-level render I/O: ffprobe, ffmpeg helpers, asset server, artifacts.

This module owns everything that touches files, ffmpeg, and the localhost
asset server. It deliberately knows nothing about narration text or AST
preparation, so it stays independent of the rest of the package.
"""

from __future__ import annotations

import base64
import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from mcp.types import (
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    ResourceLink,
    TextContent,
    TextResourceContents,
)

from .app_ui import (
    MANIM_RENDER_APP_MIME_TYPE,
    manim_render_artifact_uri,
    manim_render_resource_meta,
)
from .config import (
    ALLOWED_FORMATS,
    ASSET_ROUTE_PREFIX,
    ASSET_ROUTE_TOKEN,
    MEDIA_MIME_TYPES,
    QUALITY_FLAGS,
    RENDER_ROOT,
    VIDEO_FORMATS,
    python_executable,
    tail,
    tool_env,
)


# ---------------------------------------------------------------------------
# 1. ffprobe wrappers
# ---------------------------------------------------------------------------

def probe_media_duration(path: Path, timeout_seconds: int = 30) -> float | None:
    ffprobe = shutil.which("ffprobe", path=tool_env().get("PATH"))
    if not ffprobe:
        return None
    completed = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True, timeout=timeout_seconds, shell=False, env=tool_env(),
    )
    if completed.returncode != 0:
        return None
    try:
        return float(completed.stdout.strip())
    except ValueError:
        return None


def probe_media_streams(path: Path, timeout_seconds: int = 30) -> list[dict[str, Any]] | None:
    ffprobe = shutil.which("ffprobe", path=tool_env().get("PATH"))
    if not ffprobe:
        return None
    completed = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=timeout_seconds, shell=False, env=tool_env(),
    )
    if completed.returncode != 0:
        return None
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    streams = data.get("streams")
    return streams if isinstance(streams, list) else None


def video_stream_dimensions(path: Path) -> tuple[int, int] | None:
    streams = probe_media_streams(path)
    if not streams:
        return None
    for stream in streams:
        if stream.get("codec_type") != "video":
            continue
        width = stream.get("width")
        height = stream.get("height")
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            return width, height
    return None


def extract_raw_frame(path: Path, timestamp: float, width: int, height: int) -> bytes | None:
    ffmpeg = shutil.which("ffmpeg", path=tool_env().get("PATH"))
    if not ffmpeg:
        return None
    completed = subprocess.run(
        [
            ffmpeg, "-v", "error",
            "-ss", f"{timestamp:.3f}",
            "-i", str(path),
            "-frames:v", "1",
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "pipe:1",
        ],
        capture_output=True, timeout=30, shell=False, env=tool_env(),
    )
    expected_size = width * height * 3
    if completed.returncode != 0 or len(completed.stdout) < expected_size:
        return None
    return completed.stdout[:expected_size]


# ---------------------------------------------------------------------------
# 2. Manim command + subprocess env
# ---------------------------------------------------------------------------

def validate_render_options(quality: str, output_format: str, timeout_seconds: int) -> None:
    if quality not in QUALITY_FLAGS:
        raise ValueError(f"Unsupported quality '{quality}'. Use one of: {', '.join(QUALITY_FLAGS)}.")
    if output_format not in ALLOWED_FORMATS:
        raise ValueError(f"Unsupported format '{output_format}'. Use one of: {', '.join(sorted(ALLOWED_FORMATS))}.")
    if timeout_seconds < 1 or timeout_seconds > 600:
        raise ValueError("timeout_seconds must be between 1 and 600.")


def build_manim_command(
    script_path: Path,
    scene_name: str,
    media_dir: Path,
    log_dir: Path,
    quality: str = "low",
    output_format: str = "mp4",
    save_last_frame: bool = False,
    python_exec: str | None = None,
    verbosity: str = "warning",
) -> list[str]:
    validate_render_options(quality, output_format, 1)
    command = [
        python_exec or python_executable(),
        "-m", "manim",
        QUALITY_FLAGS[quality],
        "--format", output_format,
        "--media_dir", str(media_dir),
        "--log_dir", str(log_dir),
        "--verbosity", verbosity,
        "--progress_bar", "none",
        "--disable_caching",
        "--output_file", scene_name,
    ]
    if save_last_frame or output_format == "png":
        command.append("--save_last_frame")
    command.extend([str(script_path), scene_name])
    return command


def render_env(job_dir: Path) -> dict[str, str]:
    env = tool_env()
    env["PYTHONUNBUFFERED"] = "1"
    env["MANIM_MCP_JOB_DIR"] = str(job_dir)
    return env


# ---------------------------------------------------------------------------
# 3. Render-error parsing
# ---------------------------------------------------------------------------

def extract_render_error_summary(stderr: str | bytes | None) -> str | None:
    """Pull the useful exception message out of Manim/Rich traceback output."""
    text = tail(stderr, max_chars=20_000)
    if not text.strip():
        return None
    error_pattern = re.compile(r"^(?:[A-Za-z_][\w.]*Error|Exception|TypeError|ValueError|NameError): .+")
    raw_lines = text.splitlines()
    cleaned_lines = [line.strip(" │╭╮╰╯─") for line in raw_lines]

    def _capture(start_index: int, lines: list[str]) -> str:
        primary = lines[start_index]
        # Capture up to 2 indented continuation lines so multi-line errors
        # like "TypeError: Animation.__init__() got an unexpected keyword
        # argument 'color'" + a contextual followup are kept intact.
        captured = [primary]
        for i in range(start_index + 1, min(start_index + 3, len(lines))):
            line = lines[i]
            if not line:
                break
            if error_pattern.match(line):
                break
            captured.append(line)
        return " | ".join(captured)[:600]

    for index in range(len(cleaned_lines) - 1, -1, -1):
        if error_pattern.match(cleaned_lines[index]):
            return _capture(index, cleaned_lines)

    for raw_line in reversed(raw_lines):
        line = raw_line.strip()
        if line:
            return line[:500]
    return None


def write_text(path: Path, content: str | bytes | None) -> None:
    path.write_text(tail(content, max_chars=1_000_000), encoding="utf-8")


# ---------------------------------------------------------------------------
# 4. Asset server (localhost stream of render artifacts)
# ---------------------------------------------------------------------------

_ASSET_SERVER: ThreadingHTTPServer | None = None
_ASSET_SERVER_THREAD: threading.Thread | None = None


class _RenderAssetHandler(BaseHTTPRequestHandler):
    server_version = "ManimMCPAsset/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_HEAD(self) -> None:
        self._serve_asset(head_only=True)

    def do_GET(self) -> None:
        self._serve_asset(head_only=False)

    def _serve_asset(self, *, head_only: bool) -> None:
        parsed = urlparse(self.path)
        route_prefix = f"{ASSET_ROUTE_PREFIX}/{ASSET_ROUTE_TOKEN}/"
        if not parsed.path.startswith(route_prefix):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        relative_url = parsed.path[len(route_prefix):]
        try:
            relative_path = Path(unquote(relative_url))
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise ValueError("path traversal is not allowed")
            root = RENDER_ROOT.resolve()
            target = (root / relative_path).resolve()
            if target != root and root not in target.parents:
                raise ValueError("path is outside render root")
        except Exception:
            self.send_error(HTTPStatus.FORBIDDEN)
            return

        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        file_size = target.stat().st_size
        content_type = MEDIA_MIME_TYPES.get(
            target.suffix.lower().lstrip("."),
            mimetypes.guess_type(target.name)[0] or "application/octet-stream",
        )
        start = 0
        end = file_size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range")

        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or file_size == 0:
                self._send_range_not_satisfiable(file_size)
                return
            start_text, end_text = match.groups()
            if not start_text and not end_text:
                self._send_range_not_satisfiable(file_size)
                return
            if start_text:
                start = int(start_text)
                end = int(end_text) if end_text else file_size - 1
            else:
                suffix_length = int(end_text)
                start = max(file_size - suffix_length, 0)
                end = file_size - 1
            end = min(end, file_size - 1)
            if start >= file_size or end < start:
                self._send_range_not_satisfiable(file_size)
                return
            status = HTTPStatus.PARTIAL_CONTENT

        content_length = max(0, end - start + 1) if file_size else 0
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()

        if head_only or content_length == 0:
            return

        with target.open("rb") as file:
            file.seek(start)
            remaining = content_length
            while remaining > 0:
                chunk = file.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _send_range_not_satisfiable(self, file_size: int) -> None:
        self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
        self.send_header("Content-Range", f"bytes */{file_size}")
        self.send_header("Content-Length", "0")
        self.end_headers()


def _ensure_asset_server() -> str:
    global _ASSET_SERVER, _ASSET_SERVER_THREAD
    if _ASSET_SERVER is None:
        RENDER_ROOT.mkdir(parents=True, exist_ok=True)
        _ASSET_SERVER = ThreadingHTTPServer(("127.0.0.1", 0), _RenderAssetHandler)
        _ASSET_SERVER_THREAD = threading.Thread(
            target=_ASSET_SERVER.serve_forever,
            name="manim-mcp-render-assets",
            daemon=True,
        )
        _ASSET_SERVER_THREAD.start()
    host, port = _ASSET_SERVER.server_address[:2]
    return f"http://{host}:{port}"


def render_asset_url(path: Path) -> str | None:
    try:
        root = RENDER_ROOT.resolve()
        resolved = path.resolve()
        if resolved != root and root not in resolved.parents:
            return None
        relative = resolved.relative_to(root).as_posix()
    except Exception:
        return None
    base_url = _ensure_asset_server()
    return f"{base_url}{ASSET_ROUTE_PREFIX}/{ASSET_ROUTE_TOKEN}/{quote(relative, safe='/')}"


# ---------------------------------------------------------------------------
# 5. Artifact discovery (with narrated-tie-breaker)
# ---------------------------------------------------------------------------

def discover_artifacts(media_dir: Path, max_items: int = 20) -> list[dict[str, Any]]:
    if not media_dir.exists():
        return []
    artifacts: list[dict[str, Any]] = []
    for path in media_dir.rglob("*"):
        if "partial_movie_files" in path.parts:
            continue
        if not path.is_file() or path.suffix.lower().lstrip(".") not in ALLOWED_FORMATS:
            continue
        media_format = path.suffix.lower().lstrip(".")
        artifacts.append(
            {
                "path": str(path.resolve()),
                "uri": path.resolve().as_uri(),
                "relative_path": str(path.relative_to(media_dir)),
                "format": media_format,
                "mime_type": MEDIA_MIME_TYPES.get(media_format, "application/octet-stream"),
                "size_bytes": path.stat().st_size,
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
                "_narrated_priority": 1 if "_narrated" in path.stem else 0,
            }
        )
    # Newest first; on a tie, the file containing "_narrated" wins because it
    # is the muxed final output we want to surface to the user.
    artifacts.sort(
        key=lambda item: (item["modified_at"], item["_narrated_priority"]),
        reverse=True,
    )
    for artifact in artifacts:
        artifact.pop("_narrated_priority", None)
    return artifacts[:max_items]


def primary_video_artifact(artifacts: list[dict[str, Any]]) -> dict[str, Any] | None:
    # Prefer narrated mp4s when present.
    narrated = [a for a in artifacts if a.get("format") in VIDEO_FORMATS and "_narrated" in Path(a["path"]).stem]
    if narrated:
        return narrated[0]
    for artifact in artifacts:
        if artifact.get("format") in VIDEO_FORMATS:
            return artifact
    return None


# ---------------------------------------------------------------------------
# 6. Preview HTML + access metadata + UI resource
# ---------------------------------------------------------------------------

def create_preview_html(metadata: dict[str, Any]) -> dict[str, Any] | None:
    artifacts = metadata.get("artifacts") or []
    if not artifacts:
        return None

    primary = primary_video_artifact(artifacts) or artifacts[0]
    job_dir = Path(metadata["job_dir"])
    preview_path = job_dir / "preview.html"
    title = f"Manim render: {metadata.get('scene_name', 'scene')}"
    media_uri = render_asset_url(Path(primary["path"])) or primary["uri"]
    media_type = primary.get("mime_type", "application/octet-stream")
    media_name = Path(primary["path"]).name
    file_uri = primary["uri"]
    file_path = str(Path(primary["path"]).resolve())

    if media_type.startswith("video/") or primary.get("format") in {"mp4", "mov", "webm"}:
        media_markup = (
            f'<video controls playsinline preload="metadata" '
            f'src="{html.escape(media_uri)}" type="{html.escape(media_type)}"></video>'
        )
    elif media_type.startswith("image/"):
        media_markup = f'<img src="{html.escape(media_uri)}" alt="{html.escape(media_name)}">'
    else:
        media_markup = (
            f'<p><a href="{html.escape(media_uri)}">{html.escape(media_name)}</a></p>'
        )

    artifact_links = "\n".join(
        "<li>"
        f'<a href="{html.escape(render_asset_url(Path(artifact["path"])) or artifact["uri"])}">'
        f'{html.escape(Path(artifact["path"]).name)}</a>'
        f' <span>{html.escape(artifact.get("mime_type", "application/octet-stream"))}, '
        f'{artifact.get("size_bytes", 0)} bytes</span>'
        "</li>"
        for artifact in artifacts
    )

    preview_html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{
      margin: 0; min-height: 100vh;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #101214; color: #f5f7fa;
      display: grid; place-items: center;
    }}
    main {{ width: min(960px, calc(100vw - 32px)); padding: 24px 0; }}
    video, img {{
      width: 100%; max-height: 72vh; background: #000;
      border-radius: 8px; box-shadow: 0 16px 48px rgba(0,0,0,0.35);
    }}
    h1 {{ margin: 0 0 16px; font-size: 20px; font-weight: 650; }}
    ul {{ margin: 16px 0 0; padding-left: 20px; color: #cbd2da; font-size: 14px; line-height: 1.55; }}
    .paths {{ margin-top: 14px; color: #aeb7c2; font-size: 12px; line-height: 1.45; overflow-wrap: anywhere; }}
    a {{ color: #8ec5ff; }}
    span {{ color: #8f9aa6; }}
    code {{ color: #d7e8ff; background: rgba(255,255,255,0.08); border-radius: 4px; padding: 1px 5px; }}
  </style>
</head>
<body>
  <main>
    <h1>{html.escape(title)}</h1>
    {media_markup}
    <ul>
      {artifact_links}
    </ul>
    <div class="paths">
      <div>Stream URL: <a href="{html.escape(media_uri)}">{html.escape(media_uri)}</a></div>
      <div>File URI: <a href="{html.escape(file_uri)}">{html.escape(file_uri)}</a></div>
      <div>File path: <code>{html.escape(file_path)}</code></div>
    </div>
  </main>
</body>
</html>
"""
    preview_path.write_text(preview_html, encoding="utf-8")
    preview = {
        "path": str(preview_path.resolve()),
        "uri": preview_path.resolve().as_uri(),
        "mime_type": "text/html",
        "size_bytes": preview_path.stat().st_size,
    }
    metadata["preview_html"] = preview
    return preview


def update_access_metadata(
    metadata: dict[str, Any],
    primary: dict[str, Any] | None,
    preview: dict[str, Any] | None,
) -> dict[str, Any]:
    access: dict[str, Any] = {}
    ui_preview = metadata.get("ui_preview") or {}
    if ui_preview.get("uri"):
        access["ui_uri"] = ui_preview["uri"]

    if primary:
        video_path = Path(primary["path"])
        stream_url = render_asset_url(video_path)
        job_id = metadata.get("job_id")
        access.update({
            "video_path": str(video_path.resolve()),
            "video_file_uri": primary["uri"],
            "video_mime_type": primary.get("mime_type", "application/octet-stream"),
            "video_size_bytes": primary.get("size_bytes"),
        })
        if isinstance(job_id, str) and job_id:
            access["video_resource_uri"] = manim_render_artifact_uri(job_id)
        if stream_url:
            access["video_stream_url"] = stream_url

    if preview:
        preview_path = Path(preview["path"])
        preview_stream_url = render_asset_url(preview_path)
        access.update({
            "preview_html_path": str(preview_path.resolve()),
            "preview_html_uri": preview["uri"],
        })
        if preview_stream_url:
            access["preview_stream_url"] = preview_stream_url

    metadata["access"] = access
    return access


def _render_access_lines(access: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if access.get("video_stream_url"):
        lines.append(f"Open video: {access['video_stream_url']}")
    if access.get("preview_stream_url"):
        lines.append(f"Open player: {access['preview_stream_url']}")
    if access.get("video_path"):
        lines.append(f"Video path: {access['video_path']}")
    return lines


def update_final_response_metadata(
    metadata: dict[str, Any],
    access: dict[str, Any],
    *,
    status_text: str,
) -> str:
    access_lines = _render_access_lines(access)
    lines = [status_text, "", *access_lines]
    if metadata.get("job_id"):
        lines.append(f"Job ID: {metadata['job_id']}")
    final_response = "\n".join(line for line in lines if line is not None).strip()
    metadata["final_response_markdown"] = final_response
    metadata["claude_response_instructions"] = (
        "Include final_response_markdown verbatim in the next assistant message. "
        "Do not say only that the render is ready; the user needs the access lines."
    )
    return final_response


def create_ui_preview_resource(
    metadata: dict[str, Any],
    *,
    max_inline_video_bytes: int = 0,
) -> EmbeddedResource | None:
    artifacts = metadata.get("artifacts") or []
    if not artifacts:
        return None

    primary = primary_video_artifact(artifacts) or artifacts[0]
    media_type = primary.get("mime_type", "application/octet-stream")
    media_path = Path(primary["path"])
    served_media_uri = render_asset_url(media_path)
    linked_media_uri = served_media_uri or primary["uri"]
    ui_uri = f"ui://manim/render/{metadata.get('job_id', 'latest')}"
    title = f"Manim render: {metadata.get('scene_name', 'scene')}"
    used_inline_media = False

    if (
        (media_type.startswith("video/") or primary.get("format") in {"mp4", "mov", "webm"})
        and primary.get("size_bytes", 0) <= max_inline_video_bytes
    ):
        encoded = base64.b64encode(media_path.read_bytes()).decode("ascii")
        media_src = f"data:{media_type};base64,{encoded}"
        used_inline_media = True
        media_markup = (
            f'<video id="render" controls playsinline preload="metadata" '
            f'src="{media_src}" type="{html.escape(media_type)}"></video>'
        )
    elif media_type.startswith("image/") and primary.get("size_bytes", 0) <= max_inline_video_bytes:
        encoded = base64.b64encode(media_path.read_bytes()).decode("ascii")
        media_src = f"data:{media_type};base64,{encoded}"
        used_inline_media = True
        media_markup = f'<img id="render" src="{media_src}" alt="{html.escape(media_path.name)}">'
    elif media_type.startswith("video/") or primary.get("format") in {"mp4", "mov", "webm"}:
        media_markup = (
            f'<video id="render" controls playsinline preload="metadata" '
            f'src="{html.escape(linked_media_uri)}" type="{html.escape(media_type)}"></video>'
        )
    elif media_type.startswith("image/"):
        media_markup = f'<img id="render" src="{html.escape(linked_media_uri)}" alt="{html.escape(media_path.name)}">'
    else:
        media_markup = f'<a href="{html.escape(linked_media_uri)}">{html.escape(media_path.name)}</a>'

    fallback_link = html.escape(primary["uri"])
    if used_inline_media:
        status = "Inline media data is embedded below."
    elif served_media_uri:
        status = "Media is streamed from the local Manim MCP server."
    else:
        status = "Media is loaded from the local render artifact."
    ui_html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ margin: 0; padding: 16px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0f1115; color: #f6f7fb; }}
    main {{ max-width: 920px; margin: 0 auto; }}
    video, img {{ width: 100%; max-height: 70vh; background: #000;
      border-radius: 8px; box-shadow: 0 12px 36px rgba(0,0,0,0.34); }}
    header {{ display: flex; justify-content: space-between; gap: 12px; align-items: baseline; margin-bottom: 12px; }}
    h1 {{ margin: 0; font-size: 18px; font-weight: 650; }}
    p {{ color: #b8c0cc; font-size: 13px; line-height: 1.45; margin: 12px 0 0; }}
    a {{ color: #8ec5ff; }}
    code {{ color: #d7e8ff; background: rgba(255,255,255,0.08); border-radius: 4px; padding: 1px 5px; }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>{html.escape(title)}</h1>
      <span><code>{html.escape(str(metadata.get("job_id", "")))}</code></span>
    </header>
    {media_markup}
    <p>{html.escape(status)} Fallback MP4: <a href="{fallback_link}">{html.escape(media_path.name)}</a></p>
  </main>
</body>
</html>
"""
    metadata["ui_preview"] = {
        "uri": ui_uri,
        "mime_type": MANIM_RENDER_APP_MIME_TYPE,
        "inline_media": used_inline_media,
        "inline_limit_bytes": max_inline_video_bytes,
        "media_uri": "inline" if used_inline_media else linked_media_uri,
        "uses_asset_server": bool(served_media_uri and not used_inline_media),
    }
    return EmbeddedResource(
        type="resource",
        resource=TextResourceContents(
            uri=ui_uri,
            mimeType=MANIM_RENDER_APP_MIME_TYPE,
            _meta=manim_render_resource_meta(),
            text=ui_html,
        ),
    )


# ---------------------------------------------------------------------------
# 7. Tool result builder
# ---------------------------------------------------------------------------

def render_scene_tool_result(
    metadata: dict[str, Any],
    *,
    include_resource_links: bool = True,
    include_ui_resource: bool = False,
    embed_preview_html: bool = False,
    embed_video_bytes: bool = False,
    max_inline_video_bytes: int = 2_000_000,
    max_inline_ui_video_bytes: int = 0,
) -> CallToolResult:
    success = bool(metadata.get("success"))
    content: list[TextContent | ResourceLink | EmbeddedResource] = []

    if success:
        artifacts = metadata.get("artifacts") or []
        primary = primary_video_artifact(artifacts) or (artifacts[0] if artifacts else None)
        preview = metadata.get("preview_html")
        narration = metadata.get("narration")
        ui_resource = (
            create_ui_preview_resource(metadata, max_inline_video_bytes=max_inline_ui_video_bytes)
            if include_ui_resource
            else None
        )
        access = update_access_metadata(metadata, primary, preview)
        final_response = update_final_response_metadata(
            metadata,
            access,
            status_text=f"Your Manim render `{metadata.get('scene_name')}` is ready.",
        )
        summary_lines = [
            "MANIM RENDER ACCESS - include this block in your reply to the user:",
            final_response,
            "",
            f"Rendered `{metadata.get('scene_name')}` successfully.",
            f"Job ID: `{metadata.get('job_id')}`",
        ]
        if narration:
            video = narration.get("video", {})
            duration = video.get("output_duration_seconds") or video.get("audio_duration_seconds")
            duration_text = f" ({duration:.2f}s)" if isinstance(duration, (int, float)) else ""
            summary_lines.append(f"Narration: added{duration_text}.")
        elif not metadata.get("narration_requested"):
            summary_lines.append("Narration: not requested, so this render is silent.")
        content.append(TextContent(type="text", text="\n".join(summary_lines)))

        if ui_resource:
            content.append(ui_resource)

        if include_resource_links and primary and access.get("video_stream_url"):
            content.append(
                ResourceLink(
                    type="resource_link",
                    name=Path(primary["path"]).name,
                    title=f"{metadata.get('scene_name')} app video stream",
                    uri=access["video_stream_url"],
                    description="Localhost video stream for opening inside MCP clients.",
                    mimeType=primary.get("mime_type", "application/octet-stream"),
                    size=primary.get("size_bytes"),
                )
            )

        if include_resource_links and preview and access.get("preview_stream_url"):
            content.append(
                ResourceLink(
                    type="resource_link",
                    name="player.html",
                    title=f"{metadata.get('scene_name')} app video player",
                    uri=access["preview_stream_url"],
                    description="Localhost HTML player for opening inside MCP clients.",
                    mimeType="text/html",
                    size=preview.get("size_bytes"),
                )
            )

        if include_resource_links and primary:
            content.append(
                ResourceLink(
                    type="resource_link",
                    name=Path(primary["path"]).name,
                    title=f"{metadata.get('scene_name')} render",
                    uri=primary["uri"],
                    description="Final Manim render artifact.",
                    mimeType=primary.get("mime_type", "application/octet-stream"),
                    size=primary.get("size_bytes"),
                )
            )

        if embed_preview_html and preview:
            preview_text = Path(preview["path"]).read_text(encoding="utf-8")
            content.append(
                EmbeddedResource(
                    type="resource",
                    resource=TextResourceContents(
                        uri=preview["uri"],
                        mimeType="text/html",
                        text=preview_text,
                    ),
                )
            )

        if embed_video_bytes and primary and primary.get("size_bytes", 0) <= max_inline_video_bytes:
            video_blob = base64.b64encode(Path(primary["path"]).read_bytes()).decode("ascii")
            content.append(
                EmbeddedResource(
                    type="resource",
                    resource=BlobResourceContents(
                        uri=primary["uri"],
                        mimeType=primary.get("mime_type", "application/octet-stream"),
                        blob=video_blob,
                    ),
                )
            )
    else:
        message = metadata.get("error") or "Render failed."
        if metadata.get("violations"):
            message += "\n" + "\n".join(f"- {violation}" for violation in metadata["violations"])
        artifacts = metadata.get("artifacts") or []
        primary = primary_video_artifact(artifacts) or (artifacts[0] if artifacts else None)
        preview = metadata.get("preview_html")
        ui_resource = (
            create_ui_preview_resource(metadata, max_inline_video_bytes=max_inline_ui_video_bytes)
            if include_ui_resource and primary
            else None
        )
        access = update_access_metadata(metadata, primary, preview) if primary else {}
        if access:
            final_response = update_final_response_metadata(
                metadata,
                access,
                status_text="The Manim render completed with issues, but the video artifact is available.",
            )
            extra_lines = [
                "MANIM RENDER ACCESS - include this block in your reply to the user:",
                final_response,
            ]
            message += "\n\nCompleted artifact links:\n" + "\n".join(extra_lines)
        content.append(TextContent(type="text", text=message))

        if ui_resource:
            content.append(ui_resource)

        if include_resource_links and primary and access.get("video_stream_url"):
            content.append(
                ResourceLink(
                    type="resource_link",
                    name=Path(primary["path"]).name,
                    title=f"{metadata.get('scene_name')} app video stream",
                    uri=access["video_stream_url"],
                    description="Localhost video stream for opening inside MCP clients.",
                    mimeType=primary.get("mime_type", "application/octet-stream"),
                    size=primary.get("size_bytes"),
                )
            )
        if include_resource_links and primary:
            content.append(
                ResourceLink(
                    type="resource_link",
                    name=Path(primary["path"]).name,
                    title=f"{metadata.get('scene_name')} render artifact",
                    uri=primary["uri"],
                    description="Final Manim render artifact.",
                    mimeType=primary.get("mime_type", "application/octet-stream"),
                    size=primary.get("size_bytes"),
                )
            )

    return CallToolResult(content=content, structuredContent=metadata, isError=not success)


# ---------------------------------------------------------------------------
# 8. Job-dir helpers (used by recovery/list tools)
# ---------------------------------------------------------------------------

def latest_render_job_dir() -> Path | None:
    if not RENDER_ROOT.exists():
        return None
    job_dirs = [
        job_dir
        for job_dir in RENDER_ROOT.iterdir()
        if job_dir.is_dir() and (job_dir / "metadata.json").exists()
    ]
    if not job_dirs:
        return None
    return max(job_dirs, key=lambda item: item.stat().st_mtime)


def python_executable_path() -> str:
    return sys.executable


__all__ = [
    "build_manim_command",
    "create_preview_html",
    "create_ui_preview_resource",
    "discover_artifacts",
    "extract_raw_frame",
    "extract_render_error_summary",
    "latest_render_job_dir",
    "primary_video_artifact",
    "probe_media_duration",
    "probe_media_streams",
    "python_executable_path",
    "render_asset_url",
    "render_env",
    "render_scene_tool_result",
    "update_access_metadata",
    "update_final_response_metadata",
    "validate_render_options",
    "video_stream_dimensions",
    "write_text",
]
