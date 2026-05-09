from __future__ import annotations

import ast
import base64
import builtins
import html
import importlib.metadata
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, unquote, urlparse

from mcp.server.fastmcp import FastMCP
from mcp.types import (
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    ResourceLink,
    TextContent,
    TextResourceContents,
)


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
MAX_CODE_CHARS = 200_000
JOB_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[a-f0-9]{8}$")
WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)?")
TIMELINE_SEGMENT_METHODS = {"play_segment", "wait_segment"}

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
DEFAULT_NARRATION_MODEL = "hexgrad/Kokoro-82M"
DEFAULT_NARRATION_PROVIDER = "fal-ai"
LOCAL_NARRATION_PROVIDER = "local-kokoro"
DEFAULT_LOCAL_NARRATION_VOICE = "af_heart"
DEFAULT_LOCAL_NARRATION_LANG_CODE = "a"
LOCAL_NARRATION_PROVIDERS = {"local", "local-kokoro", "kokoro"}
RESERVED_NARRATION_HELPER_NAMES = {
    "NARRATION_TIMING",
    "MANIM_MCP_CALL_DURATIONS",
    "MANIM_MCP_CALL_DURATION_INDEX",
    "NarrationTimeline",
    "_manim_mcp_next_duration",
    "fit_to_safe_frame",
    "keep_in_safe_frame",
    "narration_timeline",
}
TERM_STOPWORDS = {
    "about",
    "above",
    "after",
    "again",
    "against",
    "along",
    "also",
    "and",
    "are",
    "because",
    "before",
    "below",
    "between",
    "but",
    "can",
    "down",
    "each",
    "finally",
    "for",
    "from",
    "give",
    "goes",
    "how",
    "inside",
    "into",
    "its",
    "let",
    "like",
    "more",
    "not",
    "now",
    "off",
    "one",
    "onto",
    "our",
    "out",
    "over",
    "put",
    "see",
    "several",
    "show",
    "step",
    "that",
    "the",
    "then",
    "this",
    "through",
    "together",
    "too",
    "under",
    "use",
    "using",
    "what",
    "when",
    "where",
    "while",
    "with",
    "working",
    "you",
}
VISUAL_TERM_IGNORE = TERM_STOPWORDS | {
    "add",
    "align",
    "animate",
    "arrow",
    "arrows",
    "arrange",
    "axis",
    "blue",
    "buff",
    "center",
    "circle",
    "color",
    "corner",
    "create",
    "dark",
    "dot",
    "edge",
    "fade",
    "fadein",
    "fadeout",
    "fill",
    "font",
    "green",
    "group",
    "height",
    "label",
    "labels",
    "lag",
    "laggedstart",
    "left",
    "line",
    "mobject",
    "move",
    "next",
    "opacity",
    "orange",
    "origin",
    "play",
    "point",
    "purple",
    "radius",
    "rectangle",
    "red",
    "right",
    "roundedrectangle",
    "scale",
    "scene",
    "segment",
    "self",
    "shift",
    "size",
    "square",
    "start",
    "stroke",
    "text",
    "title",
    "up",
    "vgroup",
    "white",
    "width",
    "write",
    "yellow",
}
ASSET_ROUTE_PREFIX = "/render-assets"
ASSET_ROUTE_TOKEN = secrets.token_urlsafe(18)
_ASSET_SERVER: ThreadingHTTPServer | None = None
_ASSET_SERVER_THREAD: threading.Thread | None = None
_LOCAL_KOKORO_PIPELINES: dict[tuple[str, str | None], Any] = {}
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
    "glob",
    "http",
    "httpx",
    "importlib",
    "multiprocessing",
    "os",
    "pathlib",
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

mcp = FastMCP("manim")


def _extra_tool_paths() -> list[str]:
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


def _tool_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    paths = _extra_tool_paths()
    if paths:
        env["PATH"] = os.pathsep.join([*paths, env.get("PATH", "")])
    return env


def _env_value(name: str) -> str | None:
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


def _version_for_distribution(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _run_probe(command: list[str], timeout: int = 5) -> dict[str, Any]:
    env = _tool_env()
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


@mcp.tool()
def check_environment() -> dict[str, Any]:
    """Check local Manim MCP dependencies without installing anything."""
    checks: dict[str, Any] = {
        "python": {
            "available": True,
            "executable": sys.executable,
            "version": sys.version.split()[0],
        },
        "uv": _run_probe(["uv", "--version"]),
        "mcp_sdk": {
            "available": _version_for_distribution("mcp") is not None,
            "version": _version_for_distribution("mcp"),
        },
        "huggingface_hub": {
            "available": _version_for_distribution("huggingface-hub") is not None,
            "version": _version_for_distribution("huggingface-hub"),
        },
        "kokoro": {
            "available": _version_for_distribution("kokoro") is not None,
            "version": _version_for_distribution("kokoro"),
        },
        "soundfile": {
            "available": _version_for_distribution("soundfile") is not None,
            "version": _version_for_distribution("soundfile"),
        },
        "espeakng_loader": {
            "available": _version_for_distribution("espeakng-loader") is not None,
            "version": _version_for_distribution("espeakng-loader"),
        },
        "espeak_ng": _run_probe(["espeak-ng", "--version"]),
        "hf_token": {
            "available": bool(_env_value("HF_TOKEN")),
            "env_var": "HF_TOKEN",
            "source": "environment or project .env",
        },
        "manim_package": {
            "available": _version_for_distribution("manim") is not None,
            "version": _version_for_distribution("manim"),
        },
        "manim_cli": _run_probe([sys.executable, "-m", "manim", "--version"], timeout=30),
        "ffmpeg": _run_probe(["ffmpeg", "-version"]),
        "ffprobe": _run_probe(["ffprobe", "-version"]),
        "pkg_config": _run_probe(["pkg-config", "--version"]),
        "cairo_pkg_config": _run_probe(["pkg-config", "--exists", "cairo"]),
        "cairo_trace": _run_probe(["cairo-trace", "--version"]),
        "latex": _run_probe(["latex", "--version"]),
        "pdflatex": _run_probe(["pdflatex", "--version"]),
        "dvisvgm": _run_probe(["dvisvgm", "--version"]),
    }
    required = ["python", "mcp_sdk", "manim_package", "manim_cli"]
    recommended = ["ffmpeg", "ffprobe", "pkg_config", "cairo_pkg_config"]
    missing_required = [name for name in required if not checks[name].get("available")]
    missing_recommended = [name for name in recommended if not checks[name].get("available")]
    notes = []
    if not checks["uv"].get("available"):
        notes.append("uv is optional for installed packages, but useful for source checkout workflows.")
    if missing_recommended:
        notes.append("Missing native tools may prevent Manim install or video rendering on macOS.")
    if not checks["ffprobe"].get("available"):
        notes.append("ffprobe is required for reliable narration/video duration sync.")
    tex_compiler_available = checks["latex"].get("available") or checks["pdflatex"].get("available")
    tex_ready = tex_compiler_available and checks["dvisvgm"].get("available")
    if not tex_ready:
        notes.append("LaTeX and dvisvgm are optional unless scenes use Tex or MathTex.")
    if not checks["hf_token"].get("available"):
        if checks["kokoro"].get("available") and checks["soundfile"].get("available"):
            notes.append("HF_TOKEN is not set; narrated renders will use local Kokoro TTS.")
        else:
            notes.append("HF_TOKEN is not set and local Kokoro TTS packages are missing.")
    return {
        "ok": not missing_required,
        "missing_required": missing_required,
        "missing_recommended": missing_recommended,
        "tex_ready": tex_ready,
        "checks": checks,
        "notes": notes,
    }


class SafetyVisitor(ast.NodeVisitor):
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
            root_name = _root_name(node.func.value)
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
    visitor = SafetyVisitor()
    visitor.visit(tree)
    return visitor.violations


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


class ConstructNameValidator(ast.NodeVisitor):
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


def analyze_code_validation(code: str, scene_name: str) -> list[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    construct = _target_construct_function(tree, scene_name)
    if construct is None:
        return []
    validator = ConstructNameValidator({*_module_bound_names(tree), "self"})
    for statement in construct.body:
        validator.visit(statement)
    return validator.violations


def _root_name(node: ast.AST) -> str | None:
    current = node
    while isinstance(current, ast.Attribute):
        current = current.value
    if isinstance(current, ast.Name):
        return current.id
    return None


def _base_name(base: ast.AST) -> str | None:
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    if isinstance(base, ast.Subscript):
        return _base_name(base.value)
    return None


def _scene_classes(tree: ast.Module) -> tuple[list[str], list[str]]:
    all_classes: list[str] = []
    scene_classes: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            all_classes.append(node.name)
            if any((_base_name(base) in SCENE_BASE_NAMES) for base in node.bases):
                scene_classes.append(node.name)
    return all_classes, scene_classes


def infer_scene_name(code: str, scene_name: str | None = None) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ValueError(f"Syntax error on line {exc.lineno or '?'}: {exc.msg}") from exc

    all_classes, scene_classes = _scene_classes(tree)
    if scene_name:
        if all_classes and scene_name not in all_classes:
            raise ValueError(f"Scene '{scene_name}' was not found in the submitted code.")
        if scene_classes and scene_name not in scene_classes:
            raise ValueError(f"Class '{scene_name}' is not a recognized Manim Scene subclass.")
        return scene_name

    if len(scene_classes) == 1:
        return scene_classes[0]
    if not scene_classes:
        raise ValueError("Could not infer a scene name because no Scene subclass was found.")
    raise ValueError(
        "Could not infer a scene name because multiple Scene subclasses were found: "
        + ", ".join(scene_classes)
    )


def _validate_render_options(
    quality: str,
    output_format: str,
    timeout_seconds: int,
) -> tuple[str, str, int]:
    if quality not in QUALITY_FLAGS:
        raise ValueError(f"Unsupported quality '{quality}'. Use one of: {', '.join(QUALITY_FLAGS)}.")
    if output_format not in ALLOWED_FORMATS:
        raise ValueError(f"Unsupported format '{output_format}'. Use one of: {', '.join(sorted(ALLOWED_FORMATS))}.")
    if timeout_seconds < 1 or timeout_seconds > 600:
        raise ValueError("timeout_seconds must be between 1 and 600.")
    return quality, output_format, timeout_seconds


def build_manim_command(
    script_path: Path,
    scene_name: str,
    media_dir: Path,
    log_dir: Path,
    quality: str = "low",
    output_format: str = "mp4",
    save_last_frame: bool = False,
    python_executable: str | None = None,
    verbosity: str = "warning",
) -> list[str]:
    _validate_render_options(quality, output_format, 1)
    command = [
        python_executable or sys.executable,
        "-m",
        "manim",
        QUALITY_FLAGS[quality],
        "--format",
        output_format,
        "--media_dir",
        str(media_dir),
        "--log_dir",
        str(log_dir),
        "--verbosity",
        verbosity,
        "--progress_bar",
        "none",
        "--disable_caching",
        "--output_file",
        scene_name,
    ]
    if save_last_frame or output_format == "png":
        command.append("--save_last_frame")
    command.extend([str(script_path), scene_name])
    return command


def _new_job_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"


def _tail(text: str | bytes | None, max_chars: int = 6000) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return text[-max_chars:]


def extract_render_error_summary(stderr: str | bytes | None) -> str | None:
    """Pull the useful exception line out of Manim/Rich traceback output."""
    text = _tail(stderr, max_chars=20_000)
    if not text.strip():
        return None
    error_pattern = re.compile(r"^(?:[A-Za-z_][\w.]*Error|Exception): .+")
    for raw_line in reversed(text.splitlines()):
        line = raw_line.strip()
        if error_pattern.match(line):
            return line[:500]
    for raw_line in reversed(text.splitlines()):
        line = raw_line.strip(" │╭╮╰╯─")
        if error_pattern.match(line):
            return line[:500]
    for raw_line in reversed(text.splitlines()):
        line = raw_line.strip()
        if line:
            return line[:500]
    return None


def _write_text(path: Path, content: str | bytes | None) -> None:
    path.write_text(_tail(content, max_chars=1_000_000), encoding="utf-8")


class RenderAssetHandler(BaseHTTPRequestHandler):
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
        _ASSET_SERVER = ThreadingHTTPServer(("127.0.0.1", 0), RenderAssetHandler)
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
            }
        )
    artifacts.sort(key=lambda item: item["modified_at"], reverse=True)
    return artifacts[:max_items]


def _primary_video_artifact(artifacts: list[dict[str, Any]]) -> dict[str, Any] | None:
    for artifact in artifacts:
        if artifact.get("format") in VIDEO_FORMATS:
            return artifact
    return None


def _require_hf_token() -> str:
    token = _env_value("HF_TOKEN")
    if not token:
        raise ValueError("HF_TOKEN is required to generate narration audio with Hugging Face.")
    return token


def _uses_local_narration_provider(provider: str) -> bool:
    return provider.lower().strip() in LOCAL_NARRATION_PROVIDERS


def narration_tts_backend(provider: str = DEFAULT_NARRATION_PROVIDER) -> str:
    if _uses_local_narration_provider(provider):
        return LOCAL_NARRATION_PROVIDER
    if _env_value("HF_TOKEN"):
        return "huggingface-api"
    return LOCAL_NARRATION_PROVIDER


def split_narration_segments(text: str) -> list[str]:
    """Split narration into spoken timing units."""
    stripped = text.strip()
    if not stripped:
        return []

    segments: list[str] = []
    for paragraph in re.split(r"\n+", stripped):
        paragraph = re.sub(r"\s+", " ", paragraph).strip()
        if not paragraph:
            continue
        pieces = re.split(r"(?<=[.!?])\s+", paragraph)
        for piece in pieces:
            cleaned = piece.strip(" \t-")
            if cleaned:
                segments.append(cleaned)

    merged: list[str] = []
    carry = ""
    for segment in segments:
        words = WORD_RE.findall(segment)
        if carry:
            segment = f"{carry} {segment}"
            carry = ""
            words = WORD_RE.findall(segment)
        if len(words) < 3 and merged:
            merged[-1] = f"{merged[-1]} {segment}"
        elif len(words) < 3:
            carry = segment
        else:
            merged.append(segment)
    if carry:
        if merged:
            merged[-1] = f"{merged[-1]} {carry}"
        else:
            merged.append(carry)
    return merged


def estimate_spoken_seconds(text: str, *, words_per_minute: float = 155.0) -> float:
    """Estimate TTS duration for one narration segment."""
    words = WORD_RE.findall(text)
    word_seconds = len(words) / max(words_per_minute / 60.0, 0.1)
    comma_pause = 0.12 * len(re.findall(r"[,]", text))
    medium_pause = 0.18 * len(re.findall(r"[;:]", text))
    terminal_pause = 0.25 if re.search(r"[.!?]\s*$", text) else 0.08
    number_pause = 0.08 * len(re.findall(r"\b\d+(?:\.\d+)?\b", text))
    return max(0.55, word_seconds + comma_pause + medium_pause + terminal_pause + number_pause)


def build_narration_timing_plan(
    text: str,
    *,
    total_duration_seconds: float | None = None,
    words_per_minute: float = 155.0,
) -> dict[str, Any]:
    """Build a sentence-level timing plan for synchronizing Manim animations."""
    segments = split_narration_segments(text)
    if not segments:
        raise ValueError("narration_text must contain at least one spoken segment.")

    raw_durations = [
        estimate_spoken_seconds(segment, words_per_minute=words_per_minute)
        for segment in segments
    ]
    estimated_total = sum(raw_durations)
    target_total = total_duration_seconds if total_duration_seconds and total_duration_seconds > 0 else estimated_total
    scale = target_total / estimated_total if estimated_total > 0 else 1.0

    planned_segments: list[dict[str, Any]] = []
    cursor = 0.0
    for index, (segment, raw_duration) in enumerate(zip(segments, raw_durations, strict=True)):
        duration = max(0.05, raw_duration * scale)
        start = cursor
        end = target_total if index == len(segments) - 1 else min(target_total, start + duration)
        duration = max(0.05, end - start)
        words = WORD_RE.findall(segment)
        planned_segments.append(
            {
                "index": index,
                "text": segment,
                "word_count": len(words),
                "estimated_seconds": round(raw_duration, 3),
                "duration_seconds": round(duration, 3),
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
            }
        )
        cursor = end

    return {
        "segment_count": len(planned_segments),
        "word_count": sum(segment["word_count"] for segment in planned_segments),
        "estimated_total_seconds": round(estimated_total, 3),
        "target_total_seconds": round(target_total, 3),
        "timing_source": "heuristic_scaled",
        "words_per_minute": words_per_minute,
        "segments": planned_segments,
    }


def build_measured_narration_timing_plan(
    segments: list[str],
    durations: list[float],
    *,
    words_per_minute: float = 155.0,
) -> dict[str, Any]:
    """Build a timing plan from real per-segment TTS durations."""
    if not segments:
        raise ValueError("narration_text must contain at least one spoken segment.")
    if len(segments) != len(durations):
        raise ValueError("segments and durations must have the same length.")

    planned_segments: list[dict[str, Any]] = []
    cursor = 0.0
    estimated_total = 0.0
    for index, (segment, duration) in enumerate(zip(segments, durations, strict=True)):
        estimated = estimate_spoken_seconds(segment, words_per_minute=words_per_minute)
        estimated_total += estimated
        safe_duration = max(0.05, float(duration))
        start = cursor
        end = start + safe_duration
        words = WORD_RE.findall(segment)
        planned_segments.append(
            {
                "index": index,
                "text": segment,
                "word_count": len(words),
                "estimated_seconds": round(estimated, 3),
                "duration_seconds": round(safe_duration, 3),
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
            }
        )
        cursor = end

    return {
        "segment_count": len(planned_segments),
        "word_count": sum(segment["word_count"] for segment in planned_segments),
        "estimated_total_seconds": round(estimated_total, 3),
        "target_total_seconds": round(cursor, 3),
        "timing_source": "measured_tts_segments",
        "words_per_minute": words_per_minute,
        "segments": planned_segments,
    }


def _timed_call_weight(call: ast.Call, kind: str) -> float:
    if kind == "wait":
        if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, int | float):
            return max(float(call.args[0].value), 0.05)
        for keyword in call.keywords:
            if (
                keyword.arg == "duration"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, int | float)
            ):
                return max(float(keyword.value.value), 0.05)
        return 1.0

    for keyword in call.keywords:
        if (
            keyword.arg == "run_time"
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, int | float)
        ):
            return max(float(keyword.value.value), 0.05)
    return 1.0


def _self_timed_call_kind(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if not isinstance(node.func.value, ast.Name) or node.func.value.id != "self":
        return None
    if node.func.attr in {"play", "wait"}:
        return node.func.attr
    return None


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
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name.replace("_", " "))
    return _term_tokens(spaced, ignore_visual_boilerplate=True)


class _VisualTermVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.string_tokens: set[str] = set()
        self.identifier_tokens: set[str] = set()

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self.string_tokens.update(_term_tokens(node.value, ignore_visual_boilerplate=True))

    def visit_Name(self, node: ast.Name) -> None:
        self.identifier_tokens.update(_identifier_terms(node.id))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.identifier_tokens.update(_identifier_terms(node.attr))
        self.generic_visit(node)


def _visual_terms_for_nodes(nodes: list[ast.AST]) -> dict[str, Any]:
    visitor = _VisualTermVisitor()
    for node in nodes:
        visitor.visit(node)
    visual_tokens = sorted(visitor.string_tokens | visitor.identifier_tokens)
    return {
        "string_terms": sorted(visitor.string_tokens),
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


def analyze_timeline_visual_alignment(
    tree: ast.Module,
    scene_name: str,
    timing_plan: dict[str, Any],
) -> dict[str, Any]:
    """Statically compare explicit timeline visual beats with narration text."""
    construct = _target_construct_function(tree, scene_name)
    segments = timing_plan.get("segments") or []
    if construct is None or not segments:
        return {"checked": False, "issues": []}

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
        terms = _visual_terms_for_nodes(chunk)
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
    """Rename top-level user bindings that would shadow injected narration helpers."""
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

    def _rename_node(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        rename = _rename_reserved_binding(
            node.name,
            kind=f"Local{type(node).__name__}",
            line=getattr(node, "lineno", None),
        )
        if rename:
            node.name = rename["to"]
            self.renames.append(rename)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self._rename_node(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        self._rename_node(node)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        self._rename_node(node)
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


def _static_iteration_count(node: ast.AST, sequence_lengths: dict[str, int] | None = None) -> int | None:
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
                if not isinstance(arg, ast.Constant) or not isinstance(arg.value, int):
                    return None
                values.append(arg.value)
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


def _target_construct_function(tree: ast.Module, scene_name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != scene_name:
            continue
        for child in node.body:
            if isinstance(child, ast.FunctionDef) and child.name == "construct":
                return child
    return None


def allocate_timed_call_durations(
    calls: list[dict[str, Any]],
    timing_plan: dict[str, Any],
) -> list[float]:
    segment_durations = [
        float(segment["duration_seconds"])
        for segment in timing_plan.get("segments", [])
    ]
    call_count = len(calls)
    segment_count = len(segment_durations)
    if call_count == 0 or segment_count == 0:
        return []

    durations = [0.0] * call_count
    if call_count <= segment_count:
        for segment_index, segment_duration in enumerate(segment_durations):
            call_index = min(call_count - 1, int(segment_index * call_count / segment_count))
            durations[call_index] += segment_duration
        return [round(max(0.05, duration), 3) for duration in durations]

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
            durations[call_index] = segment_durations[segment_index] * weight / total_weight
    return [round(max(0.05, duration), 3) for duration in durations]


def narration_timing_helper_source(
    timing_plan: dict[str, Any],
    call_durations: list[float] | None = None,
) -> str:
    plan_literal = json.dumps(timing_plan, indent=2)
    durations_literal = json.dumps(call_durations or [])
    return f'''
NARRATION_TIMING = {plan_literal}
MANIM_MCP_CALL_DURATIONS = {durations_literal}
MANIM_MCP_CALL_DURATION_INDEX = 0


def _manim_mcp_next_duration(default=1.0):
    """Return the next measured visual-beat duration for automatic retiming."""
    global MANIM_MCP_CALL_DURATION_INDEX
    try:
        fallback = max(0.05, float(default))
    except Exception:
        fallback = 1.0
    if MANIM_MCP_CALL_DURATION_INDEX >= len(MANIM_MCP_CALL_DURATIONS):
        return fallback
    duration = MANIM_MCP_CALL_DURATIONS[MANIM_MCP_CALL_DURATION_INDEX]
    MANIM_MCP_CALL_DURATION_INDEX += 1
    try:
        return max(0.05, float(duration))
    except Exception:
        return fallback


MANIM_MCP_TIMELINE_EVENTS = []
MANIM_MCP_OUTSIDE_TIMED_EVENTS = []
MANIM_MCP_IN_TIMELINE_CALL = 0
MANIM_MCP_IN_OUTSIDE_WAIT = 0


def _manim_mcp_scene_time(scene):
    try:
        return float(getattr(scene, "time", 0.0))
    except Exception:
        return 0.0


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
        _manim_mcp_record_event(
            MANIM_MCP_OUTSIDE_TIMED_EVENTS,
            dict(
                kind="play",
                line=None,
                animation_count=len(args),
                requested_run_time=_manim_mcp_json_value(kwargs.get("run_time")),
                start_seconds=start,
                end_seconds=end,
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
        requested = args[0] if args else kwargs.get("duration")
        _manim_mcp_record_event(
            MANIM_MCP_OUTSIDE_TIMED_EVENTS,
            dict(
                kind="wait",
                line=None,
                requested_duration=_manim_mcp_json_value(requested),
                start_seconds=start,
                end_seconds=end,
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
        with open(path, "w", encoding="utf-8") as file:
            _manim_mcp_json.dump(payload, file, indent=2)
            file.write("\\n")
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
            return dict(index=index, text="", duration_seconds=0.05, out_of_range=True)
        segments = self.plan.get("segments", [])
        if 0 <= index < len(segments):
            return segments[index]
        return dict(index=index, text="", duration_seconds=0.05, out_of_range=True)

    def duration(self, index, *, minimum=0.05):
        return max(minimum, float(self.segment(index)["duration_seconds"]))

    def play_segment(self, index, *animations, hold=0.0, **kwargs):
        global MANIM_MCP_IN_TIMELINE_CALL
        segment = self.segment(index)
        target_duration = max(0.05, float(segment["duration_seconds"]))
        run_time = max(0.05, self.duration(index) - max(0.0, hold))
        start = _manim_mcp_scene_time(self.scene)
        MANIM_MCP_IN_TIMELINE_CALL += 1
        try:
            if animations:
                self.scene.play(*animations, run_time=run_time, **kwargs)
            else:
                self.scene.wait(run_time)
            if hold > 0:
                self.scene.wait(hold)
        finally:
            MANIM_MCP_IN_TIMELINE_CALL -= 1
        end = _manim_mcp_scene_time(self.scene)
        _manim_mcp_record_event(
            MANIM_MCP_TIMELINE_EVENTS,
            dict(
                kind="play_segment",
                segment_index=segment.get("index", index),
                out_of_range=bool(segment.get("out_of_range")),
                segment_text=segment.get("text", ""),
                target_duration_seconds=target_duration,
                requested_run_time=run_time,
                hold_seconds=max(0.0, hold),
                animation_count=len(animations),
                start_seconds=start,
                end_seconds=end,
            ),
        )

    def wait_segment(self, index, *, scale=1.0):
        global MANIM_MCP_IN_TIMELINE_CALL
        segment = self.segment(index)
        target_duration = max(0.05, self.duration(index) * scale)
        start = _manim_mcp_scene_time(self.scene)
        MANIM_MCP_IN_TIMELINE_CALL += 1
        try:
            self.scene.wait(target_duration)
        finally:
            MANIM_MCP_IN_TIMELINE_CALL -= 1
        end = _manim_mcp_scene_time(self.scene)
        _manim_mcp_record_event(
            MANIM_MCP_TIMELINE_EVENTS,
            dict(
                kind="wait_segment",
                segment_index=segment.get("index", index),
                out_of_range=bool(segment.get("out_of_range")),
                segment_text=segment.get("text", ""),
                target_duration_seconds=target_duration,
                requested_scale=scale,
                animation_count=0,
                start_seconds=start,
                end_seconds=end,
            ),
        )


def narration_timeline(scene):
    return NarrationTimeline(scene)


def fit_to_safe_frame(mobject, *, width_ratio=0.88, height_ratio=0.82):
    """Scale a mobject or group so it stays inside the visible Manim frame."""
    max_width = config.frame_width * width_ratio
    max_height = config.frame_height * height_ratio
    scale_factors = []
    if getattr(mobject, "width", 0) and mobject.width > max_width:
        scale_factors.append(max_width / mobject.width)
    if getattr(mobject, "height", 0) and mobject.height > max_height:
        scale_factors.append(max_height / mobject.height)
    if scale_factors:
        mobject.scale(min(scale_factors))
    return mobject


def keep_in_safe_frame(mobject, *, buff=0.35):
    """Nudge a mobject back inside the visible Manim frame without scaling it."""
    left = -config.frame_width / 2 + buff
    right = config.frame_width / 2 - buff
    bottom = -config.frame_height / 2 + buff
    top = config.frame_height / 2 - buff
    if mobject.get_left()[0] < left:
        mobject.shift(RIGHT * (left - mobject.get_left()[0]))
    if mobject.get_right()[0] > right:
        mobject.shift(LEFT * (mobject.get_right()[0] - right))
    if mobject.get_bottom()[1] < bottom:
        mobject.shift(UP * (bottom - mobject.get_bottom()[1]))
    if mobject.get_top()[1] > top:
        mobject.shift(DOWN * (mobject.get_top()[1] - top))
    return mobject
'''.strip()


def _insert_after_future_imports(code: str, insertion: str) -> str:
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


def prepare_narrated_scene_code(
    code: str,
    *,
    scene_name: str,
    timing_plan: dict[str, Any],
    sync_mode: NarrationSyncMode,
) -> tuple[str, dict[str, Any]]:
    helper = narration_timing_helper_source(timing_plan)
    report: dict[str, Any] = {
        "sync_mode": sync_mode,
        "helper_injected": True,
        "scene_retimed": False,
        "timed_call_count": 0,
        "allocated_seconds": 0.0,
    }

    if sync_mode != "timeline":
        return _insert_after_future_imports(code, helper), report

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        report["warning"] = f"Could not parse scene for automatic retiming: {exc}"
        return _insert_after_future_imports(code, helper), report

    reserved_renames = sanitize_reserved_narration_names(tree)
    construct = _target_construct_function(tree, scene_name)
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
        return _insert_after_future_imports(ast.unparse(tree), helper), report

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
        return _insert_after_future_imports(ast.unparse(tree), helper), report

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
        return _insert_after_future_imports(ast.unparse(tree), helper), report

    transformer = _TimedCallTransformer(collector.calls, durations)
    construct.body = [transformer.visit(statement) for statement in construct.body]
    ast.fix_missing_locations(tree)
    report["scene_retimed"] = True
    helper = narration_timing_helper_source(timing_plan, call_durations=durations)
    return _insert_after_future_imports(ast.unparse(tree), helper), report


def _local_kokoro_pipeline(lang_code: str, repo_id: str | None) -> Any:
    cache_key = (lang_code, repo_id)
    if cache_key in _LOCAL_KOKORO_PIPELINES:
        return _LOCAL_KOKORO_PIPELINES[cache_key]

    try:
        from kokoro import KPipeline
    except Exception as exc:
        raise ValueError(
            "Local Kokoro TTS requires the Python package 'kokoro'. Run `uv sync` "
            "from the project directory."
        ) from exc

    try:
        pipeline = KPipeline(lang_code=lang_code, repo_id=repo_id)
    except Exception as exc:
        raise ValueError(
            "Local Kokoro TTS failed to initialize. Ensure the Python dependencies "
            "`kokoro`, `soundfile`, and `espeakng-loader` are installed with `uv sync`. "
            f"Original error: {exc}"
        ) from exc

    _LOCAL_KOKORO_PIPELINES[cache_key] = pipeline
    return pipeline


def _audio_chunk_to_numpy(audio: Any) -> Any:
    import numpy as np

    if hasattr(audio, "detach") and hasattr(audio, "cpu"):
        audio = audio.detach().cpu().numpy()
    return np.asarray(audio, dtype="float32")


def synthesize_local_kokoro_audio(
    text: str,
    output_path: Path,
    *,
    model: str = DEFAULT_NARRATION_MODEL,
    voice: str = DEFAULT_LOCAL_NARRATION_VOICE,
    lang_code: str = DEFAULT_LOCAL_NARRATION_LANG_CODE,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Generate narration audio with the local Kokoro package."""
    del timeout_seconds  # Kokoro runs in-process; this argument keeps call sites uniform.
    narration = text.strip()
    if not narration:
        raise ValueError("narration_text must not be empty when provided.")

    try:
        import numpy as np
        import soundfile as sf
    except Exception as exc:
        raise ValueError(
            "Local Kokoro TTS requires the Python packages 'kokoro' and 'soundfile'. "
            "Run `uv sync` from the project directory."
        ) from exc

    pipeline = _local_kokoro_pipeline(lang_code, model or None)
    chunks: list[Any] = []
    try:
        for _graphemes, _phonemes, audio in pipeline(narration, voice=voice):
            chunks.append(_audio_chunk_to_numpy(audio))
    except Exception as exc:
        raise ValueError(
            "Local Kokoro TTS failed while generating audio. If this is the first "
            "local run, Kokoro may need network access to download model weights. "
            f"Original error: {exc}"
        ) from exc

    if not chunks:
        raise ValueError("Local Kokoro TTS returned no audio chunks.")

    audio_data = chunks[0] if len(chunks) == 1 else np.concatenate(chunks)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(output_path, audio_data, 24000)
    if output_path.stat().st_size == 0:
        raise ValueError("Local Kokoro TTS wrote an empty audio file.")

    audio_duration = probe_media_duration(output_path)
    streams = probe_media_streams(output_path)
    audio_streams = [stream for stream in streams or [] if stream.get("codec_type") == "audio"]
    if streams is not None and not audio_streams:
        raise ValueError("Generated local Kokoro narration file does not contain an audio stream.")

    return {
        "path": str(output_path.resolve()),
        "uri": output_path.resolve().as_uri(),
        "model": model,
        "provider": LOCAL_NARRATION_PROVIDER,
        "backend": LOCAL_NARRATION_PROVIDER,
        "voice": voice,
        "lang_code": lang_code,
        "audio_mode": "single",
        "sample_rate": 24000,
        "chunk_count": len(chunks),
        "size_bytes": output_path.stat().st_size,
        "duration_seconds": audio_duration,
        "audio_stream_count": len(audio_streams) if streams is not None else None,
    }


def synthesize_huggingface_narration_audio(
    text: str,
    output_path: Path,
    *,
    model: str = DEFAULT_NARRATION_MODEL,
    provider: str = DEFAULT_NARRATION_PROVIDER,
    timeout_seconds: int = 120,
    retry_count: int = 2,
) -> dict[str, Any]:
    """Generate narration audio with Hugging Face Inference API."""
    narration = text.strip()
    if not narration:
        raise ValueError("narration_text must not be empty when provided.")

    token = _require_hf_token()
    try:
        from huggingface_hub import InferenceClient
    except Exception as exc:
        raise ValueError("huggingface-hub is required for narration audio.") from exc

    client = InferenceClient(
        provider=provider,
        api_key=token,
        timeout=timeout_seconds,
    )
    audio: bytes | None = None
    last_error: Exception | None = None
    attempts = max(1, retry_count + 1)
    for attempt in range(attempts):
        try:
            audio = client.text_to_speech(narration, model=model)
            break
        except Exception as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(min(2 ** attempt, 4))

    if audio is None:
        raise ValueError(
            f"Hugging Face TTS failed for model={model!r}, provider={provider!r}: {last_error}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(audio)
    if output_path.stat().st_size == 0:
        raise ValueError("Hugging Face TTS returned an empty audio file.")

    audio_duration = probe_media_duration(output_path)
    streams = probe_media_streams(output_path)
    audio_streams = [stream for stream in streams or [] if stream.get("codec_type") == "audio"]
    if streams is not None and not audio_streams:
        raise ValueError("Generated narration file does not contain an audio stream.")

    return {
        "path": str(output_path.resolve()),
        "uri": output_path.resolve().as_uri(),
        "model": model,
        "provider": provider,
        "backend": "huggingface-api",
        "audio_mode": "single",
        "size_bytes": output_path.stat().st_size,
        "duration_seconds": audio_duration,
        "audio_stream_count": len(audio_streams) if streams is not None else None,
    }


def synthesize_narration_audio(
    text: str,
    output_path: Path,
    *,
    model: str = DEFAULT_NARRATION_MODEL,
    provider: str = DEFAULT_NARRATION_PROVIDER,
    timeout_seconds: int = 120,
    retry_count: int = 2,
) -> dict[str, Any]:
    """Generate narration audio with hosted HF TTS or local Kokoro fallback."""
    if narration_tts_backend(provider) == LOCAL_NARRATION_PROVIDER:
        return synthesize_local_kokoro_audio(
            text,
            output_path,
            model=model,
            timeout_seconds=timeout_seconds,
        )
    return synthesize_huggingface_narration_audio(
        text,
        output_path,
        model=model,
        provider=provider,
        timeout_seconds=timeout_seconds,
        retry_count=retry_count,
    )


def concatenate_audio_segments(
    segment_paths: list[Path],
    output_path: Path,
    *,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Concatenate per-segment audio files into one narration track."""
    if not segment_paths:
        raise ValueError("At least one narration segment is required.")
    ffmpeg = shutil.which("ffmpeg", path=_tool_env().get("PATH"))
    if not ffmpeg:
        raise ValueError("ffmpeg is required to concatenate narration audio.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-y"]
    for path in segment_paths:
        command.extend(["-i", str(path)])

    if len(segment_paths) == 1:
        command.extend(["-vn", "-ar", "24000", "-ac", "1", str(output_path)])
    else:
        inputs = "".join(f"[{index}:a:0]" for index in range(len(segment_paths)))
        command.extend(
            [
                "-filter_complex",
                f"{inputs}concat=n={len(segment_paths)}:v=0:a=1[a]",
                "-map",
                "[a]",
                "-ar",
                "24000",
                "-ac",
                "1",
                str(output_path),
            ]
        )

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        shell=False,
        env=_tool_env(),
    )
    if completed.returncode != 0:
        raise ValueError(f"ffmpeg narration concat failed: {_tail(completed.stderr, 2000)}")

    output_duration = probe_media_duration(output_path)
    output_streams = probe_media_streams(output_path)
    output_audio_streams = [
        stream for stream in output_streams or [] if stream.get("codec_type") == "audio"
    ]
    if output_streams is not None and not output_audio_streams:
        raise ValueError("Concatenated narration file does not contain an audio stream.")

    return {
        "path": str(output_path.resolve()),
        "uri": output_path.resolve().as_uri(),
        "size_bytes": output_path.stat().st_size,
        "duration_seconds": output_duration,
        "audio_stream_count": len(output_audio_streams) if output_streams is not None else None,
        "command": command,
        "stdout_tail": _tail(completed.stdout),
        "stderr_tail": _tail(completed.stderr),
    }


def align_segmented_audio_to_timeline(
    audio: dict[str, Any],
    timeline_actual: dict[str, Any],
    output_path: Path,
    *,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Place each spoken segment at its actual rendered timeline start."""
    segment_infos = audio.get("segments") or []
    timeline_events = timeline_actual.get("timeline_events") or []
    if not segment_infos or not timeline_events:
        return audio

    ffmpeg = shutil.which("ffmpeg", path=_tool_env().get("PATH"))
    if not ffmpeg:
        raise ValueError("ffmpeg is required to align narration audio to timeline events.")

    starts: dict[int, float] = {}
    for event in timeline_events:
        if event.get("out_of_range"):
            continue
        index = event.get("segment_index")
        start = event.get("start_seconds")
        if not isinstance(index, int) or not isinstance(start, int | float):
            continue
        starts[index] = min(starts.get(index, float(start)), float(start))

    if not starts:
        return audio

    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-y"]
    input_labels: list[str] = []
    silence_gaps: list[dict[str, Any]] = []
    cursor = 0.0
    input_index = 0

    for segment in sorted(segment_infos, key=lambda item: int(item.get("index", 0))):
        index = int(segment.get("index", 0))
        segment_path = Path(segment["path"])
        segment_duration = float(segment.get("duration_seconds") or 0.0)
        start = max(0.0, float(starts.get(index, cursor)))
        gap = max(0.0, start - cursor)
        if gap > 0.02:
            command.extend(
                [
                    "-f",
                    "lavfi",
                    "-t",
                    f"{gap:.3f}",
                    "-i",
                    "anullsrc=r=24000:cl=mono",
                ]
            )
            input_labels.append(f"[{input_index}:a:0]")
            silence_gaps.append(
                {
                    "before_segment_index": index,
                    "duration_seconds": round(gap, 3),
                    "start_seconds": round(cursor, 3),
                    "end_seconds": round(start, 3),
                }
            )
            input_index += 1
            cursor = start

        command.extend(["-i", str(segment_path)])
        input_labels.append(f"[{input_index}:a:0]")
        input_index += 1
        cursor = max(cursor, start) + max(segment_duration, 0.0)

    if not input_labels:
        return audio

    command.extend(
        [
            "-filter_complex",
            f"{''.join(input_labels)}concat=n={len(input_labels)}:v=0:a=1[a]",
            "-map",
            "[a]",
            "-ar",
            "24000",
            "-ac",
            "1",
            str(output_path),
        ]
    )
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        shell=False,
        env=_tool_env(),
    )
    if completed.returncode != 0:
        raise ValueError(f"ffmpeg timeline audio alignment failed: {_tail(completed.stderr, 2000)}")

    output_duration = probe_media_duration(output_path)
    output_streams = probe_media_streams(output_path)
    output_audio_streams = [
        stream for stream in output_streams or [] if stream.get("codec_type") == "audio"
    ]
    if output_streams is not None and not output_audio_streams:
        raise ValueError("Timeline-aligned narration file does not contain an audio stream.")

    aligned = dict(audio)
    aligned.update(
        {
            "path": str(output_path.resolve()),
            "uri": output_path.resolve().as_uri(),
            "source_path": audio.get("path"),
            "source_uri": audio.get("uri"),
            "timeline_aligned": True,
            "timeline_alignment_source": "manim_scene_time",
            "timeline_silence_gaps": silence_gaps,
            "timeline_silence_gap_count": len(silence_gaps),
            "timeline_silence_total_seconds": round(
                sum(gap["duration_seconds"] for gap in silence_gaps),
                3,
            ),
            "size_bytes": output_path.stat().st_size,
            "duration_seconds": output_duration or round(cursor, 3),
            "audio_stream_count": len(output_audio_streams)
            if output_streams is not None
            else None,
            "alignment_command": command,
            "alignment_stdout_tail": _tail(completed.stdout),
            "alignment_stderr_tail": _tail(completed.stderr),
        }
    )
    return aligned


def synthesize_segmented_narration_audio(
    text: str,
    narration_dir: Path,
    *,
    model: str = DEFAULT_NARRATION_MODEL,
    provider: str = DEFAULT_NARRATION_PROVIDER,
    timeout_seconds: int = 120,
    concat_timeout_seconds: int = 300,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Generate one TTS file per narration segment and concatenate them."""
    segments = split_narration_segments(text)
    if not segments:
        raise ValueError("narration_text must contain at least one spoken segment.")

    segments_dir = narration_dir / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)
    segment_infos: list[dict[str, Any]] = []
    durations: list[float] = []
    segment_paths: list[Path] = []

    for index, segment in enumerate(segments):
        segment_path = segments_dir / f"{index:03d}.wav"
        audio_info = synthesize_narration_audio(
            segment,
            segment_path,
            model=model,
            provider=provider,
            timeout_seconds=timeout_seconds,
        )
        duration = audio_info.get("duration_seconds")
        if not isinstance(duration, int | float) or duration <= 0:
            raise ValueError(f"Could not measure duration for narration segment {index}.")
        segment_infos.append(
            {
                "index": index,
                "text": segment,
                "path": audio_info["path"],
                "uri": audio_info["uri"],
                "size_bytes": audio_info["size_bytes"],
                "duration_seconds": round(float(duration), 3),
                "audio_stream_count": audio_info.get("audio_stream_count"),
            }
        )
        durations.append(float(duration))
        segment_paths.append(Path(audio_info["path"]))

    output_path = narration_dir / "narration.wav"
    concat_info = concatenate_audio_segments(
        segment_paths,
        output_path,
        timeout_seconds=concat_timeout_seconds,
    )
    timing_plan = build_measured_narration_timing_plan(segments, durations)
    measured_total = sum(durations)
    concat_duration = concat_info.get("duration_seconds")
    duration_delta = (
        float(concat_duration) - measured_total
        if isinstance(concat_duration, int | float)
        else None
    )

    audio = {
        "path": concat_info["path"],
        "uri": concat_info["uri"],
        "model": model,
        "provider": provider,
        "audio_mode": "segmented",
        "size_bytes": concat_info["size_bytes"],
        "duration_seconds": concat_info.get("duration_seconds") or round(measured_total, 3),
        "audio_stream_count": concat_info.get("audio_stream_count"),
        "segment_count": len(segment_infos),
        "segments": segment_infos,
        "measured_segments_total_seconds": round(measured_total, 3),
        "concat_duration_delta_seconds": round(duration_delta, 3)
        if duration_delta is not None
        else None,
        "concat_command": concat_info["command"],
        "concat_stdout_tail": concat_info["stdout_tail"],
        "concat_stderr_tail": concat_info["stderr_tail"],
    }
    return audio, timing_plan


def _prepared_narration_path(prepared_narration_id: str) -> Path:
    if not JOB_ID_RE.match(prepared_narration_id):
        raise ValueError("prepared_narration_id is not valid.")
    root = PREPARED_NARRATION_ROOT.resolve()
    path = (PREPARED_NARRATION_ROOT / prepared_narration_id).resolve()
    if path != root and root not in path.parents:
        raise ValueError("prepared_narration_id resolved outside prepared narration root.")
    return path


def prepare_narration_metadata(
    narration_text: str,
    *,
    narration_model: str = DEFAULT_NARRATION_MODEL,
    narration_provider: str = DEFAULT_NARRATION_PROVIDER,
    narration_tts_timeout_seconds: int = 120,
    narration_mux_timeout_seconds: int = 300,
    narration_audio_mode: NarrationAudioMode = "segmented",
) -> dict[str, Any]:
    """Generate narration audio and measured timings without rendering a scene."""
    text = narration_text.strip()
    if not text:
        raise ValueError("narration_text must not be empty.")
    if narration_audio_mode not in {"segmented", "single"}:
        raise ValueError("narration_audio_mode must be one of: segmented, single.")

    prepared_id = _new_job_id()
    narration_dir = PREPARED_NARRATION_ROOT / prepared_id
    audio_dir = narration_dir / "audio"
    narration_dir.mkdir(parents=True, exist_ok=False)
    audio_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = narration_dir / "metadata.json"
    timing_path = narration_dir / "timing_plan.json"
    started = time.monotonic()

    metadata: dict[str, Any] = {
        "success": False,
        "prepared_narration_id": prepared_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "narration_text": text,
        "narration_text_chars": len(text),
        "narration_model": narration_model,
        "narration_provider": narration_provider,
        "narration_tts_backend": narration_tts_backend(narration_provider),
        "narration_audio_mode": narration_audio_mode,
        "prepared_narration_dir": str(narration_dir.resolve()),
        "timing_path": str(timing_path.resolve()),
    }

    try:
        if narration_audio_mode == "segmented":
            audio, timing_plan = synthesize_segmented_narration_audio(
                text,
                audio_dir,
                model=narration_model,
                provider=narration_provider,
                timeout_seconds=narration_tts_timeout_seconds,
                concat_timeout_seconds=narration_mux_timeout_seconds,
            )
        else:
            output_path = audio_dir / "narration.wav"
            audio = synthesize_narration_audio(
                text,
                output_path,
                model=narration_model,
                provider=narration_provider,
                timeout_seconds=narration_tts_timeout_seconds,
            )
            timing_plan = build_narration_timing_plan(
                text,
                total_duration_seconds=audio.get("duration_seconds"),
            )
        timing_path.write_text(json.dumps(timing_plan, indent=2) + "\n", encoding="utf-8")
        metadata.update(
            {
                "success": True,
                "duration_seconds": round(time.monotonic() - started, 3),
                "audio": audio,
                "timing_plan": timing_plan,
                "usage": (
                    "Use this prepared_narration_id with render_scene_with_prepared_narration. "
                    "Write one coherent Manim Scene, use the returned segment durations to pace "
                    "visual beats, and let the server mux the prepared audio."
                ),
            }
        )
    except Exception as exc:
        metadata.update(
            {
                "success": False,
                "duration_seconds": round(time.monotonic() - started, 3),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata


def load_prepared_narration(prepared_narration_id: str) -> dict[str, Any]:
    narration_dir = _prepared_narration_path(prepared_narration_id)
    metadata_path = narration_dir / "metadata.json"
    if not metadata_path.exists():
        raise ValueError(f"Prepared narration {prepared_narration_id!r} was not found.")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Prepared narration {prepared_narration_id!r} metadata is invalid.") from exc
    if not metadata.get("success"):
        raise ValueError(
            f"Prepared narration {prepared_narration_id!r} is not usable: "
            f"{metadata.get('error') or 'preparation failed'}"
        )
    if not metadata.get("audio") or not metadata.get("timing_plan"):
        raise ValueError(f"Prepared narration {prepared_narration_id!r} is missing audio or timing data.")
    return metadata


def probe_media_duration(path: Path, timeout_seconds: int = 30) -> float | None:
    ffprobe = shutil.which("ffprobe", path=_tool_env().get("PATH"))
    if not ffprobe:
        return None
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        shell=False,
        env=_tool_env(),
    )
    if completed.returncode != 0:
        return None
    try:
        return float(completed.stdout.strip())
    except ValueError:
        return None


def probe_media_streams(path: Path, timeout_seconds: int = 30) -> list[dict[str, Any]] | None:
    ffprobe = shutil.which("ffprobe", path=_tool_env().get("PATH"))
    if not ffprobe:
        return None
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        shell=False,
        env=_tool_env(),
    )
    if completed.returncode != 0:
        return None
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    streams = data.get("streams")
    return streams if isinstance(streams, list) else None


def mux_narration_audio(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    *,
    sync_mode: NarrationSyncMode = "fit",
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Mux narration into an MP4, with configurable video/audio duration sync."""
    ffmpeg = shutil.which("ffmpeg", path=_tool_env().get("PATH"))
    if not ffmpeg:
        raise ValueError("ffmpeg is required to mux narration audio into the video.")

    video_duration = probe_media_duration(video_path)
    audio_duration = probe_media_duration(audio_path)
    duration_delta = None
    extra_duration = 0.0
    if video_duration is not None and audio_duration is not None:
        duration_delta = audio_duration - video_duration
        extra_duration = max(0.0, audio_duration - video_duration)

    if sync_mode in {"timeline", "fit", "pad"} and (
        video_duration is None or audio_duration is None
    ):
        missing = []
        if video_duration is None:
            missing.append("video")
        if audio_duration is None:
            missing.append("audio")
        raise ValueError(
            "Could not measure "
            + " and ".join(missing)
            + " duration with ffprobe. Narration sync requires ffprobe; without it, "
            "ffmpeg can produce a video whose last frame sits idle while narration continues."
        )

    if (
        sync_mode == "fit"
        and video_duration is not None
        and audio_duration is not None
        and video_duration > 0
        and abs(audio_duration - video_duration) > 0.1
    ):
        video_pts_factor = max(audio_duration / video_duration, 0.05)
        command = [
            ffmpeg,
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-filter_complex",
            f"[0:v]setpts={video_pts_factor:.8f}*PTS[v]",
            "-map",
            "[v]",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        mode = "fit_video_to_audio"
    elif extra_duration > 0.1:
        command = [
            ffmpeg,
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-filter_complex",
            f"[0:v]tpad=stop_mode=clone:stop_duration={extra_duration:.3f},setpts=PTS-STARTPTS[v]",
            "-map",
            "[v]",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        mode = "extend_video"
    elif (
        video_duration is not None
        and audio_duration is not None
        and video_duration - audio_duration > 0.1
    ):
        command = [
            ffmpeg,
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-filter_complex",
            f"[1:a:0]apad=whole_dur={video_duration:.3f}[a]",
            "-map",
            "0:v:0",
            "-map",
            "[a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        mode = "pad_audio"
    else:
        command = [
            ffmpeg,
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        mode = "mux_audio"

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        shell=False,
        env=_tool_env(),
    )
    if completed.returncode != 0:
        raise ValueError(f"ffmpeg narration mux failed: {_tail(completed.stderr, 2000)}")

    output_duration = probe_media_duration(output_path)
    output_streams = probe_media_streams(output_path)
    output_audio_streams = [
        stream for stream in output_streams or [] if stream.get("codec_type") == "audio"
    ]
    if output_streams is not None and not output_audio_streams:
        raise ValueError("Narrated MP4 was created, but ffprobe found no audio stream in it.")

    return {
        "path": str(output_path.resolve()),
        "uri": output_path.resolve().as_uri(),
        "mime_type": "video/mp4",
        "size_bytes": output_path.stat().st_size,
        "mode": mode,
        "sync_strategy": mode,
        "requested_sync_mode": sync_mode,
        "video_duration_seconds": video_duration,
        "audio_duration_seconds": audio_duration,
        "duration_delta_seconds": duration_delta,
        "output_duration_seconds": output_duration,
        "output_audio_stream_count": len(output_audio_streams)
        if output_streams is not None
        else None,
        "command": command,
        "stdout_tail": _tail(completed.stdout),
        "stderr_tail": _tail(completed.stderr),
    }


def _video_stream_dimensions(path: Path) -> tuple[int, int] | None:
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


def _extract_raw_frame(path: Path, timestamp: float, width: int, height: int) -> bytes | None:
    ffmpeg = shutil.which("ffmpeg", path=_tool_env().get("PATH"))
    if not ffmpeg:
        return None
    completed = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        capture_output=True,
        timeout=30,
        shell=False,
        env=_tool_env(),
    )
    expected_size = width * height * 3
    if completed.returncode != 0 or len(completed.stdout) < expected_size:
        return None
    return completed.stdout[:expected_size]


def _background_rgb(frame: bytes, width: int, height: int) -> tuple[int, int, int]:
    samples: list[bytes] = []
    coords = [
        (0, 0),
        (width - 1, 0),
        (0, height - 1),
        (width - 1, height - 1),
        (width // 2, 0),
        (0, height // 2),
        (width - 1, height // 2),
        (width // 2, height - 1),
    ]
    for x, y in coords:
        index = (y * width + x) * 3
        samples.append(frame[index:index + 3])
    return tuple(
        int(sorted(sample[channel] for sample in samples)[len(samples) // 2])
        for channel in range(3)
    )


def _frame_content_bounds(
    frame: bytes,
    width: int,
    height: int,
    *,
    threshold: int = 35,
    step: int = 2,
) -> dict[str, Any] | None:
    background = _background_rgb(frame, width, height)
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
    return {
        "background_rgb": background,
        "bbox": {
            "x_min": min_x,
            "y_min": min_y,
            "x_max": max_x,
            "y_max": max_y,
            "width": max_x - min_x + 1,
            "height": max_y - min_y + 1,
        },
        "sampled_content_pixels": content_pixels,
    }


def analyze_video_frame_bounds(
    video_path: Path,
    *,
    sample_count: int = 8,
    edge_margin_px: int = 12,
) -> dict[str, Any]:
    """Sample rendered frames and flag content touching the visible frame edge."""
    duration = probe_media_duration(video_path)
    dimensions = _video_stream_dimensions(video_path)
    if duration is None or not dimensions:
        return {
            "ok": False,
            "warning": "Could not measure video dimensions or duration for frame-bound checks.",
            "samples": [],
        }

    width, height = dimensions
    samples: list[dict[str, Any]] = []
    edge_hits = 0
    times = [
        duration * (index + 1) / (sample_count + 1)
        for index in range(sample_count)
        if duration > 0
    ]
    for timestamp in times:
        frame = _extract_raw_frame(video_path, timestamp, width, height)
        if frame is None:
            continue
        bounds = _frame_content_bounds(frame, width, height)
        if bounds is None:
            samples.append({"timestamp_seconds": round(timestamp, 3), "content_detected": False})
            continue
        bbox = bounds["bbox"]
        touches_edge = (
            bbox["x_min"] <= edge_margin_px
            or bbox["y_min"] <= edge_margin_px
            or bbox["x_max"] >= width - edge_margin_px - 1
            or bbox["y_max"] >= height - edge_margin_px - 1
        )
        edge_hits += int(touches_edge)
        samples.append(
            {
                "timestamp_seconds": round(timestamp, 3),
                "content_detected": True,
                "touches_edge": touches_edge,
                **bounds,
            }
        )

    checked = sum(1 for sample in samples if sample.get("content_detected"))
    edge_hit_ratio = edge_hits / checked if checked else 0.0
    return {
        "ok": edge_hits == 0,
        "width": width,
        "height": height,
        "duration_seconds": duration,
        "sample_count": len(samples),
        "content_sample_count": checked,
        "edge_touch_count": edge_hits,
        "edge_hit_ratio": round(edge_hit_ratio, 3),
        "edge_margin_px": edge_margin_px,
        "samples": samples,
    }


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
    worst_start_event: dict[str, Any] | None = None
    worst_end_event: dict[str, Any] | None = None

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
            enriched.update(
                {
                    "planned_start_seconds": round(planned_start, 3),
                    "planned_end_seconds": round(planned_end, 3),
                    "start_delta_seconds": start_delta,
                    "end_delta_seconds": end_delta,
                    "duration_delta_seconds": duration_delta,
                }
            )
            if abs(start_delta) > max_abs_start_delta:
                max_abs_start_delta = abs(start_delta)
                worst_start_event = enriched
            if abs(end_delta) > max_abs_end_delta:
                max_abs_end_delta = abs(end_delta)
                worst_end_event = enriched
        enriched_events.append(enriched)

    actual["timeline_events"] = enriched_events
    actual["sync_summary"] = {
        "max_abs_start_delta_seconds": round(max_abs_start_delta, 3),
        "max_abs_end_delta_seconds": round(max_abs_end_delta, 3),
        "worst_start_event": worst_start_event,
        "worst_end_event": worst_end_event,
    }
    return actual


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
            issues.append(
                {
                    "severity": "error",
                    "code": "dynamic_loop_timing",
                    "message": (
                        f"{dynamic_loop_calls} timed calls are inside loops with unknown iteration counts, "
                        "so they could not be aligned to narration segments. Use narration_timeline(self) "
                        "with tl.play_segment(...) / tl.wait_segment(...)."
                    ),
                }
            )

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
            if outside_timeline_calls:
                audio_timeline_aligned = bool(narration_audio.get("timeline_aligned"))
                severity = (
                    "warning"
                    if audio_timeline_aligned
                    else "error"
                    if outside_timeline_seconds > 1.5 or outside_timeline_calls > 2
                    else "warning"
                )
                issues.append(
                    {
                        "severity": severity,
                        "code": "outside_timeline_timed_calls",
                        "message": (
                            f"{outside_timeline_calls} timed self.play/self.wait calls "
                            f"({outside_timeline_seconds:.2f}s measured) are outside "
                            "tl.play_segment(...) / tl.wait_segment(...). Timeline audio "
                            "can insert silence for transition gaps, but important visual "
                            "motion should live inside the narration segment that explains it."
                        ),
                        "outside_timeline_timed_call_count": outside_timeline_calls,
                        "outside_timeline_estimated_seconds": outside_timeline_seconds,
                        "measured_from_render": bool(timeline_actual),
                        "timeline_audio_aligned": audio_timeline_aligned,
                    }
                )
            sync_summary = timeline_actual.get("sync_summary") or {}
            max_start_delta = float(sync_summary.get("max_abs_start_delta_seconds") or 0.0)
            if max_start_delta > 0.75:
                worst_event = sync_summary.get("worst_start_event") or {}
                audio_timeline_aligned = bool(narration_audio.get("timeline_aligned"))
                issues.append(
                    {
                        "severity": "warning" if audio_timeline_aligned else "error",
                        "code": "actual_timeline_start_drift",
                        "message": (
                            f"Measured render timing drifted by up to {max_start_delta:.2f}s "
                            "from the initial narration plan. Timeline-aligned audio can follow "
                            "those rendered starts, but large gaps may feel slow unless they are "
                            "intentional pauses."
                        ),
                        "max_abs_start_delta_seconds": round(max_start_delta, 3),
                        "segment_index": worst_event.get("segment_index"),
                        "start_delta_seconds": worst_event.get("start_delta_seconds"),
                        "timeline_audio_aligned": audio_timeline_aligned,
                    }
                )
            if segment_count and covered_count < segment_count and not dynamic_segment_calls:
                issues.append(
                    {
                        "severity": "error",
                        "code": "incomplete_timeline_coverage",
                        "message": (
                            f"Only {covered_count} of {segment_count} narration segments are bound "
                            "with tl.play_segment(...) or tl.wait_segment(...). Unbound narration "
                            "segments can make the animation finish early and leave the final frame "
                            "idle while audio continues."
                        ),
                        "covered_segment_count": covered_count,
                        "segment_count": segment_count,
                        "missing_segments": missing_segments,
                    }
                )
            if out_of_range_segments:
                issues.append(
                    {
                        "severity": "warning",
                        "code": "out_of_range_timeline_segment",
                        "message": (
                            "Some timeline calls use segment indices outside the narration range. "
                            "Extra timeline waits are treated as short pauses; bind narration only "
                            "to indices 0 through segment_count - 1."
                        ),
                        "segment_count": segment_count,
                        "out_of_range_segments": out_of_range_segments,
                    }
                )
            alignment = narration_sync.get("timeline_alignment") or {}
            for alignment_issue in alignment.get("issues") or []:
                severity = alignment_issue.get("severity")
                if severity not in {"error", "warning"}:
                    severity = "warning"
                issues.append(
                    {
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
                    }
                )

        audio_duration = narration_video.get("audio_duration_seconds")
        original_video_duration = narration_video.get("video_duration_seconds")
        mode = narration_video.get("mode")
        if (
            mode == "fit_video_to_audio"
            and isinstance(audio_duration, (int, float))
            and isinstance(original_video_duration, (int, float))
            and audio_duration > 0
        ):
            delta = float(audio_duration) - float(original_video_duration)
            ratio = abs(delta) / max(float(audio_duration), 0.1)
            if ratio > 0.15 and explicit_timeline:
                issues.append(
                    {
                        "severity": "error",
                        "code": "severe_timeline_duration_mismatch",
                        "message": (
                            f"The timeline-rendered video duration ({original_video_duration:.2f}s) differed from "
                            f"narration ({audio_duration:.2f}s) by {abs(delta):.2f}s. This usually means the "
                            "scene shadowed the injected narration_timeline helper or has substantial timed "
                            "self.play/self.wait calls outside the timeline."
                        ),
                        "delta_seconds": round(delta, 3),
                        "ratio": round(ratio, 3),
                    }
                )
            elif ratio > 0.15:
                issues.append(
                    {
                        "severity": "error",
                        "code": "severe_global_retime",
                        "message": (
                            f"The silent video duration ({original_video_duration:.2f}s) differed from "
                            f"narration ({audio_duration:.2f}s) by {abs(delta):.2f}s, so ffmpeg globally "
                            f"retimed the entire video by {ratio:.0%}. This usually causes poor sync."
                        ),
                        "delta_seconds": round(delta, 3),
                        "ratio": round(ratio, 3),
                    }
                )

    primary = _primary_video_artifact(metadata.get("artifacts") or [])
    if visual_checks and primary:
        bounds = analyze_video_frame_bounds(Path(primary["path"]))
        if not bounds.get("ok") and bounds.get("edge_touch_count", 0):
            issues.append(
                {
                    "severity": "error",
                    "code": "content_touches_frame_edge",
                    "message": (
                        f"Rendered content touches the frame edge in {bounds.get('edge_touch_count')} "
                        f"of {bounds.get('content_sample_count')} sampled frames. Scale groups with "
                        "fit_to_safe_frame(...) or keep orbit/layout radii inside the visible frame."
                    ),
                    "edge_touch_count": bounds.get("edge_touch_count"),
                    "content_sample_count": bounds.get("content_sample_count"),
                    "edge_hit_ratio": bounds.get("edge_hit_ratio"),
                }
            )
        metadata["visual_bounds"] = bounds

    ok = not any(issue["severity"] == "error" for issue in issues)
    quality = {"ok": ok, "issues": issues}
    metadata["quality_checks"] = quality
    return quality


def add_narration_to_render(
    metadata: dict[str, Any],
    narration_text: str,
    *,
    model: str = DEFAULT_NARRATION_MODEL,
    provider: str = DEFAULT_NARRATION_PROVIDER,
    tts_timeout_seconds: int = 120,
    mux_timeout_seconds: int = 300,
    sync_mode: NarrationSyncMode = "fit",
    audio: dict[str, Any] | None = None,
    audio_path: Path | None = None,
) -> dict[str, Any]:
    artifacts = metadata.get("artifacts") or []
    primary = _primary_video_artifact(artifacts)
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
    metadata["narration_audio"] = audio
    muxed_video = mux_narration_audio(
        original_video_path,
        Path(audio["path"]),
        narrated_video_path,
        sync_mode=sync_mode,
        timeout_seconds=mux_timeout_seconds,
    )
    metadata["narration"] = {
        "text": narration_text,
        "audio": audio,
        "video": muxed_video,
    }
    metadata["artifacts"] = discover_artifacts(Path(metadata["media_dir"]))
    return metadata


def create_preview_html(metadata: dict[str, Any]) -> dict[str, Any] | None:
    artifacts = metadata.get("artifacts") or []
    if not artifacts:
        return None

    primary = artifacts[0]
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
      margin: 0;
      min-height: 100vh;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #101214;
      color: #f5f7fa;
      display: grid;
      place-items: center;
    }}
    main {{
      width: min(960px, calc(100vw - 32px));
      padding: 24px 0;
    }}
    video, img {{
      width: 100%;
      max-height: 72vh;
      background: #000;
      border-radius: 8px;
      box-shadow: 0 16px 48px rgba(0, 0, 0, 0.35);
    }}
    h1 {{
      margin: 0 0 16px;
      font-size: 20px;
      font-weight: 650;
      letter-spacing: 0;
    }}
    ul {{
      margin: 16px 0 0;
      padding-left: 20px;
      color: #cbd2da;
      font-size: 14px;
      line-height: 1.55;
    }}
    .paths {{
      margin-top: 14px;
      color: #aeb7c2;
      font-size: 12px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }}
    a {{ color: #8ec5ff; }}
    span {{ color: #8f9aa6; }}
    code {{
      color: #d7e8ff;
      background: rgba(255, 255, 255, 0.08);
      border-radius: 4px;
      padding: 1px 5px;
    }}
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
        access.update(
            {
                "video_path": str(video_path.resolve()),
                "video_file_uri": primary["uri"],
                "video_mime_type": primary.get("mime_type", "application/octet-stream"),
                "video_size_bytes": primary.get("size_bytes"),
            }
        )
        if stream_url:
            access["video_stream_url"] = stream_url

    if preview:
        preview_path = Path(preview["path"])
        preview_stream_url = render_asset_url(preview_path)
        access.update(
            {
                "preview_html_path": str(preview_path.resolve()),
                "preview_html_uri": preview["uri"],
            }
        )
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
    lines = [
        status_text,
        "",
        *access_lines,
    ]
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

    primary = artifacts[0]
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
    body {{
      margin: 0;
      padding: 16px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0f1115;
      color: #f6f7fb;
    }}
    main {{ max-width: 920px; margin: 0 auto; }}
    video, img {{
      width: 100%;
      max-height: 70vh;
      background: #000;
      border-radius: 8px;
      box-shadow: 0 12px 36px rgba(0, 0, 0, 0.34);
    }}
    header {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
      margin-bottom: 12px;
    }}
    h1 {{
      margin: 0;
      font-size: 18px;
      font-weight: 650;
      letter-spacing: 0;
    }}
    p {{
      color: #b8c0cc;
      font-size: 13px;
      line-height: 1.45;
      margin: 12px 0 0;
    }}
    a {{ color: #8ec5ff; }}
    code {{
      color: #d7e8ff;
      background: rgba(255, 255, 255, 0.08);
      border-radius: 4px;
      padding: 1px 5px;
    }}
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
        "mime_type": "text/html;profile=mcp-app",
        "inline_media": used_inline_media,
        "inline_limit_bytes": max_inline_video_bytes,
        "media_uri": "inline" if used_inline_media else linked_media_uri,
        "uses_asset_server": bool(served_media_uri and not used_inline_media),
    }
    return EmbeddedResource(
        type="resource",
        resource=TextResourceContents(
            uri=ui_uri,
            mimeType="text/html;profile=mcp-app",
            text=ui_html,
        ),
    )


def _render_scene_tool_result(
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
        primary = artifacts[0] if artifacts else None
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
        primary = artifacts[0] if artifacts else None
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
            if extra_lines:
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


def _render_env(job_dir: Path) -> dict[str, str]:
    env = _tool_env()
    env["PYTHONUNBUFFERED"] = "1"
    env["MANIM_MCP_JOB_DIR"] = str(job_dir)
    return env


def _render_scene_metadata(
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
    try:
        if not code.strip():
            raise ValueError("code must not be empty.")
        if len(code) > MAX_CODE_CHARS:
            raise ValueError(f"code exceeds the {MAX_CODE_CHARS} character limit.")
        _validate_render_options(quality, output_format, timeout_seconds)
        if narration_sync_mode not in {"timeline", "fit", "pad"}:
            raise ValueError("narration_sync_mode must be one of: timeline, fit, pad.")
        if narration_audio_mode not in {"segmented", "single"}:
            raise ValueError("narration_audio_mode must be one of: segmented, single.")
        if narration_text and prepared_narration_id:
            raise ValueError("Use either narration_text or prepared_narration_id, not both.")
        if prepared_narration_id:
            prepared_narration = load_prepared_narration(prepared_narration_id)
            resolved_narration_text = prepared_narration["narration_text"]
            narration_audio_mode = prepared_narration.get("narration_audio_mode", narration_audio_mode)
            narration_model = prepared_narration.get("narration_model", narration_model)
            narration_provider = prepared_narration.get("narration_provider", narration_provider)
        if resolved_narration_text and output_format != "mp4":
            raise ValueError("Narration currently requires output_format='mp4'.")

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

    job_id = _new_job_id()
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
    }

    render_code = code
    pre_render_audio: dict[str, Any] | None = None
    pre_render_audio_path: Path | None = None
    if resolved_narration_text:
        try:
            narration_dir = job_dir / "narration"
            narration_dir.mkdir(parents=True, exist_ok=True)
            pre_render_audio_path = narration_dir / "narration.wav"
            if prepared_narration:
                pre_render_audio = prepared_narration["audio"]
                pre_render_audio_path = Path(pre_render_audio["path"])
                timing_plan = prepared_narration["timing_plan"]
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
            metadata.update(
                {
                    "source_script_path": str(source_script_path.resolve()),
                    "narration_audio": pre_render_audio,
                    "narration_timing_plan": timing_plan,
                    "narration_timing_path": str(timing_path.resolve()),
                    "narration_sync": sync_report,
                }
            )
        except Exception as exc:
            script_path.write_text(code, encoding="utf-8")
            _write_text(stdout_path, "")
            _write_text(stderr_path, f"{type(exc).__name__}: {exc}")
            metadata.update(
                {
                    "success": False,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "artifacts": discover_artifacts(media_dir),
                    "stdout_tail": "",
                    "stderr_tail": f"{type(exc).__name__}: {exc}",
                    "error": f"Narration failed before render: {exc}",
                }
            )
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
            env=_render_env(job_dir),
        )
        _write_text(stdout_path, completed.stdout)
        _write_text(stderr_path, completed.stderr)
        artifacts = discover_artifacts(media_dir)
        stderr_tail = _tail(completed.stderr)
        metadata.update(
            {
                "success": completed.returncode == 0 and bool(artifacts),
                "returncode": completed.returncode,
                "duration_seconds": round(time.monotonic() - started, 3),
                "artifacts": artifacts,
                "stdout_tail": _tail(completed.stdout),
                "stderr_tail": stderr_tail,
            }
        )
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
        _write_text(stdout_path, exc.stdout)
        _write_text(stderr_path, exc.stderr)
        metadata.update(
            {
                "success": False,
                "timed_out": True,
                "duration_seconds": round(time.monotonic() - started, 3),
                "artifacts": discover_artifacts(media_dir),
                "stdout_tail": _tail(exc.stdout),
                "stderr_tail": _tail(exc.stderr),
                "error": f"Render timed out after {timeout_seconds} seconds.",
            }
        )
    except Exception as exc:
        _write_text(stdout_path, "")
        _write_text(stderr_path, f"{type(exc).__name__}: {exc}")
        metadata.update(
            {
                "success": False,
                "duration_seconds": round(time.monotonic() - started, 3),
                "artifacts": discover_artifacts(media_dir),
                "stdout_tail": "",
                "stderr_tail": f"{type(exc).__name__}: {exc}",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )

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
        quality = analyze_render_quality(metadata, visual_checks=visual_quality_checks)
        if fail_on_quality_issues and not quality["ok"]:
            issue_messages = [issue["message"] for issue in quality["issues"] if issue["severity"] == "error"]
            metadata["success"] = False
            metadata["quality_failed"] = True
            metadata["error"] = "Render completed, but quality checks failed: " + " ".join(issue_messages)
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata


@mcp.tool()
def plan_narration_timing(
    narration_text: str,
    total_duration_seconds: float | None = None,
    words_per_minute: float = 155.0,
) -> dict[str, Any]:
    """Create a sentence-level timing plan for narrated Manim scenes.

    Use this before writing a narrated scene when you want explicit sync. The
    returned segments can be paired with `narration_timeline(self)` in code that
    is later rendered by render_scene_with_narration. For final renders, the
    segmented narration audio mode will replace these heuristic durations with
    real per-sentence TTS durations. Good narrated scenes should introduce the
    topic/problem first, explain the goal, then build the answer step by step.
    """
    return build_narration_timing_plan(
        narration_text,
        total_duration_seconds=total_duration_seconds,
        words_per_minute=words_per_minute,
    )


@mcp.tool()
def prepare_narration(
    narration_text: str,
    narration_model: str = DEFAULT_NARRATION_MODEL,
    narration_provider: str = DEFAULT_NARRATION_PROVIDER,
    narration_tts_timeout_seconds: int = 120,
    narration_mux_timeout_seconds: int = 300,
    narration_audio_mode: NarrationAudioMode = "segmented",
) -> dict[str, Any]:
    """Prepare narration audio and exact segment timings before writing a scene.

    Use this optional first step for complex narrated videos. It returns a
    prepared_narration_id, combined audio path, per-sentence durations, and
    per-segment audio paths. Claude should use those durations to plan a full
    Manim scene with attractive visuals, then call
    render_scene_with_prepared_narration. Do not inject audio playback code into
    Manim; the server will mux and verify the prepared audio after render.
    """
    return prepare_narration_metadata(
        narration_text,
        narration_model=narration_model,
        narration_provider=narration_provider,
        narration_tts_timeout_seconds=narration_tts_timeout_seconds,
        narration_mux_timeout_seconds=narration_mux_timeout_seconds,
        narration_audio_mode=narration_audio_mode,
    )


@mcp.prompt(
    name="write_narrated_manim_scene",
    title="Write Narrated Manim Scene",
    description="Create a ManimCE scene that is synchronized to narration and stays inside frame.",
)
def write_narrated_manim_scene_prompt(topic: str, quality: str = "low") -> str:
    """Prompt template for synchronized narrated Manim scenes."""
    return f"""
Create and render a narrated ManimCE scene about: {topic}

For complex explanations, call prepare_narration first, use the returned
durations to plan the scene, then call render_scene_with_prepared_narration.
For quick/simple explanations, call render_scene_with_narration directly.
Use quality="{quality}", narration_sync_mode="timeline",
narration_audio_mode="segmented", visual_quality_checks=true, and
fail_on_quality_issues=true.

Rules:
- Use ManimCE normally; the MCP helpers are for timing/framing, not a replacement for Manim.
- Write narration_text first: 6 to 10 short sentences. Sentence 0 introduces the topic/problem, sentence 1 states the goal, middle sentences explain step by step, final sentence summarizes.
- In construct(), use tl = narration_timeline(self), then bind each sentence once with tl.play_segment(index, ...) or tl.wait_segment(index). The visual in segment i must depict narration sentence i; if a sentence names "fuel -> engine -> wheels", reveal that flow in the same segment.
- Keep timed self.play/self.wait calls inside tl segments; use self.add/self.remove for instant setup/cleanup. Transitions also belong inside the narration segment they support.
- Use fit_to_safe_frame(group) for wide layouts and keep_in_safe_frame(label) for floating labels/callouts.
- Make it visually engaging: use tasteful color, depth, motion, highlights, transforms, indications, camera moves, traced paths, graphs, 3D, and layered VGroups when they help. Keep each beat readable: one main idea per narration sentence, then hold on the final frame until the sentence finishes.
- Avoid common Python mistakes: do not reuse comprehension variables like i or p after a comprehension.
- Prefer Text unless LaTeX is really needed and tex_ready=true.
- If the tool returns a render or quality error, fix the reported diagnostic and rerender once. On success, include final_response_markdown verbatim.
""".strip()


@mcp.tool()
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
    """Render a complete ManimCE scene and return compact media links and metadata.

    Media bytes, preview HTML, and ui:// resources are not embedded by default
    so the MCP tool result stays below Claude Desktop's response size limits.
    The normal response includes Open video, Open player, and Video path lines.
    Set include_ui_resource, embed_preview_html, or embed_video_bytes only when
    your MCP client can render those resources usefully.

    Args:
        narration_text: Optional spoken script to mux into the rendered MP4.
            If the user asks for voice, narration, audio, or a spoken
            explanation, pass this field or use render_scene_with_narration.
            Without it, the output video is intentionally silent.
        prepared_narration_id: Optional id returned by prepare_narration. Use
            this instead of narration_text when Claude already has measured
            segment durations and wants to render with that exact prepared audio.
        narration_sync_mode: `timeline` retimes self.play/self.wait calls to a
            sentence-level plan before render, measures actual timeline starts
            during render, and aligns segmented audio to those starts. `fit`
            globally retimes the final video to the narration.
            `pad` preserves the video speed and pads/freezes only at the end.
        narration_audio_mode: `segmented` synthesizes one audio file per
            narration segment and uses real per-segment durations for tighter
            sync. `single` synthesizes one audio file and estimates segment
            timing heuristically.
        fail_on_quality_issues: When true, renders that technically complete
            but have severe sync/layout issues are returned as tool errors with
            artifact links so Claude can revise and rerender.

    Important for Claude Desktop: after this tool returns, include
    `final_response_markdown` verbatim in the assistant response. It contains
    the access lines the user needs.
    """
    metadata = _render_scene_metadata(
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
    return _render_scene_tool_result(
        metadata,
        include_resource_links=include_resource_links,
        include_ui_resource=include_ui_resource,
        embed_preview_html=embed_preview_html,
        embed_video_bytes=embed_video_bytes,
        max_inline_video_bytes=max_inline_video_bytes,
        max_inline_ui_video_bytes=max_inline_ui_video_bytes,
    )


@mcp.tool()
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
    """Render a narrated ManimCE MP4.

    Use this tool, not plain render_scene, whenever the user asks for voice,
    narration, audio, spoken explanation, or a video that explains something out
    loud. The narration text is required. The server handles audio generation,
    muxing, measured sentence durations, actual Manim Scene.time tracking, and
    audio-stream verification. In timeline mode it aligns segmented audio to
    rendered beat starts instead of globally speeding up or slowing down the
    video. Your job is to write a Manim scene whose visual beats match the
    spoken script. For complex videos, prepare_narration followed by
    render_scene_with_prepared_narration gives Claude exact segment durations
    before it writes the scene.

    For best first-render quality, write narration_text before scene code. The
    first sentence should introduce the topic/problem, the second should state
    what the video will help the viewer understand, the middle sentences should
    explain step by step, and the final sentence should summarize. Do not jump
    straight to the final formula, answer, or finished diagram.

    Use explicit timeline beats by default: call `tl = narration_timeline(self)`
    near the start of construct(), then bind every narration sentence in order
    with exactly one primary `tl.play_segment(index, ...)` or
    `tl.wait_segment(index)`. Segment i should visually depict sentence i. If a
    sentence says "fuel, engine, transmission, driveshaft, wheels", reveal that
    exact flow in that same segment; do not play it one sentence earlier or
    later. If there are N narration sentences, use only indices 0 through N-1.
    Avoid timed self.play/self.wait outside the helper; use self.add/self.remove
    for instant setup/cleanup. Short transition pauses are allowed, but
    important visual motion should happen inside the segment that explains it.
    The automatic retimer can count literal
    range/list loops and simple local list/tuple/set assignments, but complex
    loops should use explicit segment calls. Do not reuse comprehension
    variables such as `i` or `p` after the comprehension ends.
    Do not define custom helpers named `narration_timeline`,
    `NarrationTimeline`, `fit_to_safe_frame`, or `keep_in_safe_frame`; the
    server injects those.

    Make the video attractive enough to hold attention: use tasteful color,
    depth, smooth transforms, indications, camera moves, updaters, paths,
    graphs, 3D scenes, and layered groups when they help the explanation. Pace
    first drafts generously: one clear idea plus one optional label or callout
    per narration sentence is usually better than many unrelated motions.
    Fade or replace old labels before adding new ones to avoid overlays.
    For frame-safe layouts, group large systems and call fit_to_safe_frame(group)
    before animating them; keep labels within the frame with keep_in_safe_frame.
    By default, severe sync drift or clipped-frame quality checks return a tool
    error with artifact links and concrete feedback so Claude can revise and
    rerender once before responding to the user. If Manim exits with an
    exception, the tool returns the concise exception line so Claude can fix the
    scene. If the final render succeeds, do not mention internal sync warnings
    in the final response.
    After this tool returns, include `final_response_markdown` verbatim in the
    assistant response so the user can open the video from Claude Desktop.
    """
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


@mcp.tool()
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
    """Render a ManimCE MP4 using audio from prepare_narration.

    Use this after prepare_narration for higher-quality narrated videos. Claude
    should write one complete Manim scene and may use any Manim capability:
    transforms, updaters, camera movement, 3D scenes, graphs, paths, and rich
    VGroup compositions are all allowed. The prepared timing plan is guidance
    for pacing; the server still owns audio muxing, actual render-time sync
    measurement, and quality feedback.

    In timeline mode, use `tl = narration_timeline(self)` and bind narration
    sentence i to visual beat i with `tl.play_segment(i, ...)` or
    `tl.wait_segment(i)`. Make the visuals attractive and viewer-friendly, but
    keep them readable and inside the frame. On success, include
    `final_response_markdown` verbatim in the assistant response.
    """
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


def _latest_render_job_dir() -> Path | None:
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


@mcp.tool()
def get_render_access(job_id: str = "latest") -> CallToolResult:
    """Return compact video/player links for a render job.

    Use this when the user asks where the video is, when Claude forgot to show
    render links, or after rendering. Include final_response_markdown verbatim
    in the assistant response so the user can open the video from Claude.
    """
    try:
        if job_id == "latest":
            job_dir = _latest_render_job_dir()
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
    primary = _primary_video_artifact(artifacts) or (artifacts[0] if artifacts else None)
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
        "claude_response_instructions": full_metadata["claude_response_instructions"],
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


@mcp.tool()
def list_renders(limit: int = 20) -> dict[str, Any]:
    """List recent Manim MCP render jobs."""
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


def _job_dir_for(job_id: str) -> Path:
    if not JOB_ID_RE.match(job_id):
        raise ValueError("Invalid job_id format.")
    job_dir = (RENDER_ROOT / job_id).resolve()
    render_root = RENDER_ROOT.resolve()
    if render_root not in job_dir.parents and job_dir != render_root:
        raise ValueError("Invalid job_id path.")
    return job_dir


@mcp.tool()
def read_render_log(job_id: str, max_chars: int = 8000) -> dict[str, Any]:
    """Read bounded stdout and stderr logs for a render job."""
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
    return {
        "success": True,
        "job_id": job_id,
        "stdout_log": str(stdout_path.resolve()),
        "stderr_log": str(stderr_path.resolve()),
        "stdout_tail": _tail(stdout, safe_max_chars),
        "stderr_tail": _tail(stderr, safe_max_chars),
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
