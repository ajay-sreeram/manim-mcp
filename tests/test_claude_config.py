from __future__ import annotations

import json
import sys
from pathlib import Path

from manim_mcp.claude_config import build_manim_server_entry, install_claude_config, merge_claude_config


def test_default_entry_runs_installed_package_module() -> None:
    entry = build_manim_server_entry()

    assert entry == {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "manim_mcp.server"],
    }


def test_source_entry_uses_uv_project() -> None:
    entry = build_manim_server_entry(project_root=Path("/tmp/manim_mcp"), uv_path=Path("/tmp/uv"))

    assert entry == {
        "type": "stdio",
        "command": "/tmp/uv",
        "args": ["--directory", "/tmp/manim_mcp", "run", "manim-mcp"],
    }


def test_merge_preserves_existing_servers_and_preferences() -> None:
    original = {
        "mcpServers": {
            "weather": {"command": "uv", "args": ["run", "weather.py"]},
            "mmt_server": {"command": "python", "args": ["server.py"]},
        },
        "preferences": {"coworkWebSearchEnabled": True},
    }
    entry = build_manim_server_entry(project_root=Path("/tmp/manim_mcp"), uv_path=Path("/tmp/uv"))

    merged, changed = merge_claude_config(original, entry=entry)

    assert changed is True
    assert merged["mcpServers"]["weather"] == original["mcpServers"]["weather"]
    assert merged["mcpServers"]["mmt_server"] == original["mcpServers"]["mmt_server"]
    assert merged["preferences"] == original["preferences"]
    assert merged["mcpServers"]["manim"] == entry


def test_merge_is_idempotent_when_entry_matches() -> None:
    entry = build_manim_server_entry(project_root=Path("/tmp/manim_mcp"), uv_path=Path("/tmp/uv"))
    original = {"mcpServers": {"manim": entry}}

    merged, changed = merge_claude_config(original, entry=entry)

    assert changed is False
    assert merged == original


def test_install_claude_config_writes_backup_on_change(tmp_path) -> None:
    config_path = tmp_path / "claude_desktop_config.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"weather": {"command": "uv", "args": []}}}),
        encoding="utf-8",
    )
    entry = build_manim_server_entry(project_root=Path("/tmp/manim_mcp"), uv_path=Path("/tmp/uv"))

    result = install_claude_config(path=config_path, entry=entry)

    assert result["changed"] is True
    assert result["backup_path"] is not None
    assert Path(result["backup_path"]).exists()
    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert updated["mcpServers"]["weather"]["command"] == "uv"
    assert updated["mcpServers"]["manim"] == entry
