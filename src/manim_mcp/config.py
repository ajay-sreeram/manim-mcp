"""Shared configuration: paths, constants, types, and environment helpers.

Kept intentionally small so every other module can import it without cycles.
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import secrets
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Project root + render paths
# ---------------------------------------------------------------------------

def _source_project_root() -> Path | None:
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "pyproject.toml").exists() and (candidate / "src" / "manim_mcp").is_dir():
        return candidate
    return None


def _default_project_root() -> Path:
    configured = os.environ.get("MANIM_MCP_HOME")
    if configured:
        return Path(configured).expanduser()
    return _source_project_root() or (Path.home() / ".manim_mcp")


PROJECT_ROOT = _default_project_root()
RENDER_ROOT = PROJECT_ROOT / "renders"
PREPARED_NARRATION_ROOT = PROJECT_ROOT / "prepared_narrations"


# ---------------------------------------------------------------------------
# Limits + identifiers
# ---------------------------------------------------------------------------

MAX_CODE_CHARS = 200_000
# Soft cap on narration text length; we target sub-2-minute videos.
SOFT_NARRATION_CHAR_LIMIT = 2_400          # ~2 minutes at ~155 wpm
HARD_NARRATION_CHAR_LIMIT = 4_000          # absolute upper bound

JOB_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[a-f0-9]{8}$")
WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)?")


# ---------------------------------------------------------------------------
# Render quality / format
# ---------------------------------------------------------------------------

Quality = Literal["low", "medium", "high", "production", "fourk"]
OutputFormat = Literal["mp4", "png", "gif", "webm", "mov"]
NarrationSyncMode = Literal["timeline", "fit", "pad"]
NarrationAudioMode = Literal["segmented", "single"]

QUALITY_FLAGS: dict[str, str] = {
    "low": "-ql",
    "medium": "-qm",
    "high": "-qh",
    "production": "-qp",
    "fourk": "-qk",
}
ALLOWED_FORMATS = {"mp4", "png", "gif", "webm", "mov"}
MEDIA_MIME_TYPES = {
    "gif": "image/gif",
    "mov": "video/quicktime",
    "mp4": "video/mp4",
    "png": "image/png",
    "webm": "video/webm",
}
VIDEO_FORMATS = {"mp4", "mov", "webm"}


# ---------------------------------------------------------------------------
# Timing constants (the most important "feel" knobs)
# ---------------------------------------------------------------------------

# Pause inserted between consecutive narration sentences when concatenating
# raw TTS WAVs. Without this, sentences run into each other and sound robotic.
SENTENCE_GAP_SECONDS = 0.18           # comma / regular sentence break
TERMINAL_GAP_SECONDS = 0.32           # after `?` or `!`
PARAGRAPH_GAP_SECONDS = 0.55          # between paragraphs (blank line in narration_text)

# Minimum runtime for any timed call so animations never collapse to a "pop".
MIN_PLAY_RUN_TIME_SECONDS = 0.45
MIN_WAIT_DURATION_SECONDS = 0.20

# Cap on global ffmpeg `setpts` retiming when sync_mode=fit. Keeps the visuals
# from being unbearably slow or sped up if the LLM's pacing was way off.
MIN_GLOBAL_VIDEO_PTS = 0.65
MAX_GLOBAL_VIDEO_PTS = 1.55

# Hard ceiling on how long we are willing to clone the final frame to cover
# trailing audio. Beyond this we report a quality error.
MAX_VIDEO_EXTEND_SECONDS = 6.0

# Per-segment audio overflow tolerance. If a sentence's TTS audio overruns
# its rendered visual beat by more than this, we surface a quality issue.
SEGMENT_AUDIO_OVERFLOW_TOLERANCE = 0.18

# Default soft target for any single visual beat. Beats below this look choppy.
SOFT_MIN_BEAT_SECONDS = 1.0


# ---------------------------------------------------------------------------
# TTS providers / voices (single-voice Kokoro by design — minimal MCP)
# ---------------------------------------------------------------------------

DEFAULT_NARRATION_MODEL = "hexgrad/Kokoro-82M"
DEFAULT_NARRATION_PROVIDER = "fal-ai"
LOCAL_NARRATION_PROVIDER = "local-kokoro"
DEFAULT_LOCAL_NARRATION_VOICE = "af_heart"
DEFAULT_LOCAL_NARRATION_LANG_CODE = "a"
LOCAL_NARRATION_PROVIDERS = {"local", "local-kokoro", "kokoro"}


# ---------------------------------------------------------------------------
# Reserved helper names injected into user scenes
# ---------------------------------------------------------------------------

RESERVED_NARRATION_HELPER_NAMES = {
    "NARRATION_TIMING",
    "MANIM_MCP_CALL_DURATIONS",
    "MANIM_MCP_CALL_DURATION_INDEX",
    "NarrationTimeline",
    "_manim_mcp_next_duration",
    "_manim_mcp_reset_call_index",
    "fit_to_safe_frame",
    "keep_in_safe_frame",
    "narration_timeline",
}

TIMELINE_SEGMENT_METHODS = {"play_segment", "wait_segment"}


# ---------------------------------------------------------------------------
# AST safety lists (BLOCKED_MODULES intentionally allows `pathlib` so users
# can load asset files; runtime exec is still blocked via direct calls.)
# ---------------------------------------------------------------------------

SCENE_BASE_NAMES = {
    "Scene",
    "ThreeDScene",
    "MovingCameraScene",
    "ZoomedScene",
    "LinearTransformationScene",
    "VectorScene",
    "SpecialThreeDScene",
}

BLOCKED_MODULES = {
    "builtins",
    "ctypes",
    "ftplib",
    "http",
    "httpx",
    "importlib",
    "multiprocessing",
    "os",
    "pickle",
    "requests",
    "shlex",
    "shutil",
    "socket",
    "subprocess",
    "sys",
    "tempfile",
    "urllib",
    "webbrowser",
}

BLOCKED_DIRECT_CALLS = {
    "__import__",
    "breakpoint",
    "compile",
    "eval",
    "exec",
    "exit",
    "input",
    "open",
    "quit",
}

BLOCKED_MODULE_ATTR_CALLS = {
    "chmod",
    "chown",
    "execv",
    "execve",
    "execvp",
    "kill",
    "link",
    "mkdir",
    "makedirs",
    "open",
    "popen",
    "remove",
    "rename",
    "replace",
    "rmdir",
    "rmtree",
    "spawnl",
    "spawnle",
    "spawnlp",
    "spawnlpe",
    "spawnv",
    "spawnve",
    "spawnvp",
    "spawnvpe",
    "symlink",
    "system",
    "unlink",
}


# ---------------------------------------------------------------------------
# Term filters used by the visual-alignment heuristic
# ---------------------------------------------------------------------------

TERM_STOPWORDS = {
    "about", "above", "after", "again", "against", "along", "also", "and",
    "are", "because", "before", "below", "between", "but", "can", "down",
    "each", "finally", "for", "from", "give", "goes", "how", "inside",
    "into", "its", "let", "like", "more", "not", "now", "off", "one",
    "onto", "our", "out", "over", "put", "see", "several", "show", "step",
    "that", "the", "then", "this", "through", "together", "too", "under",
    "use", "using", "what", "when", "where", "while", "with", "working",
    "you",
}
VISUAL_TERM_IGNORE = TERM_STOPWORDS | {
    "add", "align", "animate", "arrow", "arrows", "arrange", "axis", "blue",
    "buff", "center", "circle", "color", "corner", "create", "dark", "dot",
    "edge", "fade", "fadein", "fadeout", "fill", "font", "green", "group",
    "height", "label", "labels", "lag", "laggedstart", "left", "line",
    "mobject", "move", "next", "opacity", "orange", "origin", "play",
    "point", "purple", "radius", "rectangle", "red", "right",
    "roundedrectangle", "scale", "scene", "segment", "self", "shift",
    "size", "square", "start", "stroke", "text", "title", "up", "vgroup",
    "white", "width", "write", "yellow",
}


# ---------------------------------------------------------------------------
# Asset server settings (regenerated each process)
# ---------------------------------------------------------------------------

ASSET_ROUTE_PREFIX = "/render-assets"
ASSET_ROUTE_TOKEN = secrets.token_urlsafe(18)


# ---------------------------------------------------------------------------
# Tool path discovery (cached) + .env reading
# ---------------------------------------------------------------------------

_EXTRA_TOOL_PATHS_CACHE: list[str] | None = None


def _discover_extra_tool_paths() -> list[str]:
    candidates: list[Path] = []
    for root in [
        PROJECT_ROOT / ".tinytex" / "bin",
        Path.home() / "Library" / "TinyTeX" / "bin",
        Path.home() / ".TinyTeX" / "bin",
    ]:
        if root.exists():
            candidates.extend(sorted(path for path in root.iterdir() if path.is_dir()))
    candidates.extend(
        [
            PROJECT_ROOT / ".texenv" / "bin",
            Path("/Library/TeX/texbin"),
        ]
    )
    return [str(path) for path in candidates if path.exists()]


def extra_tool_paths() -> list[str]:
    global _EXTRA_TOOL_PATHS_CACHE
    if _EXTRA_TOOL_PATHS_CACHE is None:
        _EXTRA_TOOL_PATHS_CACHE = _discover_extra_tool_paths()
    return list(_EXTRA_TOOL_PATHS_CACHE)


def tool_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    paths = extra_tool_paths()
    if paths:
        env["PATH"] = os.pathsep.join([*paths, env.get("PATH", "")])
    return env


def env_value(name: str) -> str | None:
    """Read an env var, falling back to the project-level .env file."""
    value = os.environ.get(name)
    if value:
        return value

    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return None

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        if key.strip() != name:
            continue
        parsed = raw_value.strip().strip('"').strip("'")
        return parsed or None
    return None


# ---------------------------------------------------------------------------
# External tool probing (used by check_environment)
# ---------------------------------------------------------------------------

def version_for_distribution(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def run_probe(command: list[str], timeout: int = 5) -> dict[str, Any]:
    env = tool_env()
    executable = shutil.which(command[0], path=env.get("PATH"))
    if not executable and not Path(command[0]).exists():
        return {"available": False, "command": command, "path": None}

    full_command = [str(Path(command[0])) if Path(command[0]).exists() else executable, *command[1:]]
    try:
        completed = subprocess.run(
            full_command,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            env=env,
        )
    except Exception as exc:
        return {
            "available": False,
            "command": command,
            "path": full_command[0],
            "error": f"{type(exc).__name__}: {exc}",
        }

    output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part)
    return {
        "available": completed.returncode == 0,
        "command": command,
        "path": full_command[0],
        "returncode": completed.returncode,
        "output": output[:2000],
    }


# ---------------------------------------------------------------------------
# Misc helpers used across the rest of the package
# ---------------------------------------------------------------------------

def new_job_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"


def tail(text: str | bytes | None, max_chars: int = 6000) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return text[-max_chars:]


def python_executable() -> str:
    return sys.executable
