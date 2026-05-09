from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CLAUDE_CONFIG_PATH = Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
DEFAULT_UV_PATH = Path(shutil.which("uv") or "uv")


def _source_project_root() -> Path | None:
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "pyproject.toml").exists() and (candidate / "src" / "manim_mcp").is_dir():
        return candidate
    return None


def build_manim_server_entry(
    project_root: Path | None = None,
    uv_path: Path | None = None,
    command: Path | str | None = None,
    args: list[str] | None = None,
) -> dict[str, Any]:
    """Build a Claude Desktop MCP server entry.

    Defaults to the installed package entry point by running the current Python
    interpreter as `python -m manim_mcp.server`. Pass project_root to create the
    old source-tree `uv --directory ... run manim-mcp` entry.
    """
    if args is not None or command is not None:
        return {
            "type": "stdio",
            "command": str(command or sys.executable),
            "args": args or ["-m", "manim_mcp.server"],
        }

    if project_root is not None:
        resolved_uv = uv_path or DEFAULT_UV_PATH
        return {
            "type": "stdio",
            "command": str(resolved_uv),
            "args": [
                "--directory",
                str(project_root),
                "run",
                "manim-mcp",
            ],
        }

    return {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "manim_mcp.server"],
    }


def merge_claude_config(
    config: dict[str, Any],
    server_name: str = "manim",
    entry: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    merged = dict(config)
    servers = dict(merged.get("mcpServers", {}))
    desired_entry = entry or build_manim_server_entry()
    changed = servers.get(server_name) != desired_entry
    servers[server_name] = desired_entry
    merged["mcpServers"] = servers
    return merged, changed or merged != config


def load_claude_config(path: Path = CLAUDE_CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"mcpServers": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def backup_config(path: Path = CLAUDE_CONFIG_PATH) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = path.with_name(f"{path.stem}.{stamp}.bak{path.suffix}")
    shutil.copy2(path, backup_path)
    return backup_path


def install_claude_config(
    path: Path = CLAUDE_CONFIG_PATH,
    server_name: str = "manim",
    entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = load_claude_config(path)
    merged, changed = merge_claude_config(config, server_name=server_name, entry=entry)

    backup_path = None
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        backup_path = backup_config(path)
        path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")

    return {
        "changed": changed,
        "config_path": str(path),
        "backup_path": str(backup_path) if backup_path else None,
        "server_name": server_name,
        "server_entry": merged["mcpServers"][server_name],
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Install the Manim MCP server in Claude Desktop config.")
    parser.add_argument("--config", type=Path, default=CLAUDE_CONFIG_PATH)
    parser.add_argument("--name", default="manim")
    parser.add_argument(
        "--source",
        action="store_true",
        help="Install a source-tree uv entry instead of the current Python package entry.",
    )
    parser.add_argument("--project-root", type=Path, default=None)
    parser.add_argument("--uv", type=Path, default=DEFAULT_UV_PATH)
    args = parser.parse_args(argv)

    entry = None
    if args.source:
        project_root = args.project_root or _source_project_root()
        if project_root is None:
            raise SystemExit("--source requires --project-root when not run from a source checkout.")
        entry = build_manim_server_entry(project_root=project_root, uv_path=args.uv)

    result = install_claude_config(path=args.config, server_name=args.name, entry=entry)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
