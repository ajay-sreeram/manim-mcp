"""Manim MCP server entry point.

The previous monolithic ``server.py`` was deprecated in 0.2.0 and replaced
by the focused modules listed below. This file now does only three things:

1. Construct the :class:`FastMCP` instance.
2. Ask :mod:`tools` to register every tool + prompt on it.
3. Provide ``main()`` for the ``manim-mcp`` console script.

Module layout (read this if you're contributing):

* :mod:`config`           -- paths, types, constants, env helpers
* :mod:`safety`           -- AST safety + name validation + reserved-name rename
* :mod:`narration`        -- sentence split, silence rules, TTS, audio align, prepare
* :mod:`scene_helpers`    -- the runtime helper source we inject into user scenes
* :mod:`scene_prepare`    -- AST retime collector/transformer + scene preparation
* :mod:`render_io`        -- ffprobe, ffmpeg, asset server, artifacts, preview HTML
* :mod:`quality`          -- frame bounds + alignment + analyze_render_quality
* :mod:`render_pipeline`  -- orchestrates code -> narration -> render -> mux -> quality
* :mod:`prompts`          -- LLM-facing copy (tool docstrings, cheat sheet, prompt)
* :mod:`tools`            -- thin @mcp.tool wrappers + register_tools(mcp)
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .tools import register_tools


mcp = FastMCP("manim")
register_tools(mcp)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
