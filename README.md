# Manim MCP

Local MCP server for Claude Desktop that renders Manim Community Edition scenes and returns compact artifact paths, logs, and metadata.

## What It Provides

- `check_environment`: reports Python, optional `uv`, MCP SDK, Manim, FFmpeg, `pkg-config`, Cairo, LaTeX, and TTS availability. It is a diagnostic tool only; it does not install anything.
- `render_scene`: renders one Manim scene from complete ManimCE Python code into a per-job folder under `renders/`.
  Successful renders return compact `Open video`, `Open player`, and `Video path` lines plus metadata. Media bytes and HTML resources are not embedded by default, keeping Claude Desktop tool results small and predictable.
  Pass `narration_text` to synthesize narration with hosted Hugging Face TTS or local Kokoro, save it as audio, and mux it into the rendered MP4.
- `render_scene_with_narration`: same renderer, but with required `narration_text`. Use this when you ask Claude for voice, narration, audio, or a spoken explanation. By default it generates one audio file per narration segment, measures each segment's real duration, records actual Manim `Scene.time` starts during render, aligns audio to those rendered beats, and verifies that the final MP4 has an audio stream. If `HF_TOKEN` is available, narration uses the hosted Hugging Face Inference API; otherwise it falls back to local Kokoro.
- `prepare_narration`: optional first step for complex narrated videos. It generates narration audio, returns exact segment durations plus compact relative audio paths such as `narration/segments/000.wav`, and gives Claude a `prepared_narration_id` to use while planning a full Manim scene.
- `render_scene_with_prepared_narration`: renders one complete Manim scene using audio/timing from `prepare_narration`. Claude still writes normal Manim code with full Manim freedom; the server owns muxing, measured sync, and quality feedback.
- `plan_narration_timing`: returns a draft sentence-level timing plan without rendering, useful when writing a scene that explicitly uses the injected `narration_timeline(self)` helper. Final segmented renders replace heuristic durations with measured TTS durations.
- `get_render_access`: returns only the compact access block for a render job, useful when Claude says a video is ready but does not show the video links. Use `job_id="latest"` for the newest render.
- Prompt `write_narrated_manim_scene`: reusable authoring prompt for Claude that asks it to create narrated scenes with explicit segment timing and frame-safe layouts.
- `list_renders`: lists recent render jobs.
- `read_render_log`: reads bounded stdout/stderr logs for a render job.

## Safety Model

This server uses hybrid guardrails, not a true sandbox. Manim scene code is still Python code. Before rendering, the server performs AST checks that block obvious high-risk imports and calls such as `os`, `subprocess`, `socket`, `open`, `eval`, and `exec`. Renders run in isolated job folders with fixed media/log paths, timeouts, and `subprocess.run(..., shell=False)`.

Treat this as suitable for trusted local experimentation, not for running arbitrary untrusted code.

## Setup

Install directly from GitHub after you publish the repository:

```bash
uv tool install "git+https://github.com/ajay-sreeram/manim-mcp.git"
manim-mcp-install-claude
```

Or with pip:

```bash
python -m pip install "git+https://github.com/ajay-sreeram/manim-mcp.git"
manim-mcp-install-claude
```

For local development from a checkout:

```bash
uv sync
uv run pytest
uv run manim-mcp-install-claude
```

Restart Claude Desktop after installing the config entry.

When installed as a package, render jobs are stored under `~/.manim_mcp/renders` and prepared narrations under `~/.manim_mcp/prepared_narrations` by default. Set `MANIM_MCP_HOME=/path/to/workdir` before starting Claude Desktop if you want a different work root. In a source checkout, these folders stay under the repository.

Narration works in two modes. If you provide a Hugging Face token through either the environment or a project-local `.env` file, the server uses the hosted Hugging Face Inference API:

```bash
echo 'HF_TOKEN=your_token_here' > .env
```

The default hosted TTS settings are:

- provider: `fal-ai`
- model: `hexgrad/Kokoro-82M`

If `HF_TOKEN` is not set, the server uses local Kokoro through the Python packages `kokoro` and `soundfile`. The first local narration may download the public Kokoro model weights and the small SpaCy English model used by Kokoro's text frontend. The default local settings are:

- provider/backend: `local-kokoro`
- model: `hexgrad/Kokoro-82M`
- voice: `af_heart`
- language code: `a`

You can force local TTS even when `HF_TOKEN` exists by passing `narration_provider="local-kokoro"`.

Narration sync modes:

- `timeline` (default): synthesize audio first, inject `NARRATION_TIMING`/`narration_timeline(self)`, and let explicit `tl.play_segment(...)` / `tl.wait_segment(...)` calls use measured spoken durations. The render records actual timeline starts and inserts silence gaps into segmented audio when the scene has real transition pauses, instead of globally speeding/slowing the video. If no explicit timeline helper is used, simple `self.play(...)`/`self.wait(...)` calls are rewritten to match the spoken timeline. Static `range(...)` loops are accounted for.
- `fit`: leave scene code alone, then retime the whole video to the narration duration.
- `pad`: preserve video speed and only pad/freeze the ending if durations differ.

Narration audio modes:

- `segmented` (default): split narration into sentences, generate TTS per segment, measure each segment, concatenate the audio, and use those exact segment durations for sync.
- `single`: generate one narration file and estimate segment timings from text length. This is faster and uses fewer API calls, but sync is less precise.

For best sync and first-render quality, write narrated Manim scenes as visual beats. Start with the narration, not the code. For complex topics, ask Claude to call `prepare_narration` first so it sees exact per-sentence durations before writing the Manim scene:

- Segment 0 introduces the topic or problem.
- Segment 1 states what the viewer is trying to understand.
- Middle segments build the explanation step by step.
- The final segment summarizes the idea.

Then bind every sentence to an explicit timeline segment. Do not jump straight to the final formula, answer, or finished diagram:

```python
class Example(Scene):
    def construct(self):
        tl = narration_timeline(self)
        earth = Circle(color=BLUE, fill_opacity=1)
        moon = Circle(radius=0.2, color=LIGHT_GREY, fill_opacity=1)

        tl.play_segment(0, FadeIn(earth))
        tl.play_segment(1, FadeIn(moon), moon.animate.move_to(RIGHT * 2))
        tl.wait_segment(2)
```

Use `self.add(...)` for instant setup if needed, but keep important timed motion inside `tl.play_segment(...)` / `tl.wait_segment(...)` in narrated scenes. Short transition waits are allowed; timeline mode measures them and can align audio around them. If there are N narration sentences, valid segment indices are `0` through `N - 1`. Do not define custom helpers named `narration_timeline`, `NarrationTimeline`, `fit_to_safe_frame`, or `keep_in_safe_frame`; the server injects them. Automatic timeline mode also understands simple static loops, including literal `range(...)` loops and loops over locally assigned list/tuple/set literals. It rewrites those animations to consume measured per-segment durations at runtime when no explicit timeline helper is used. Loops over dynamic mobjects, comprehensions, external data, or objects built by function calls cannot be reliably counted, so narrated scenes should use explicit segment indices for those cases.

For slide-like explainers, do not let visuals accumulate across subtopics. Put the visible objects for a beat in a `VGroup`, fade that group out or call `self.clear()` before building the next beat, then introduce the next visual. If submobjects were animated one by one, removing only the parent `VGroup` may leave old pieces on screen.

## Quality Gates

Narrated renders default to `fail_on_quality_issues=true`. A render can finish and still be returned as a tool error when the server detects likely bad output. The error still includes `Open video`, `Open player`, and `Video path` lines so you can inspect the artifact.

The current checks flag:

- Timed `self.play(...)` or `self.wait(...)` calls inside loops whose iteration count cannot be inferred.
- Severe global retiming, where the silent video duration differs from narration by more than 15 percent and no explicit timeline helper was used.
- Severe explicit-timeline duration mismatch, where a supposedly segment-bound scene still differs from narration by more than 15 percent.
- Visual content touching the sampled video frame edge, which usually means labels, orbits, graphs, or panels are clipped.
- Explicit timeline scenes that do not bind every narration sentence with `tl.play_segment(...)` or `tl.wait_segment(...)`.
- Out-of-range timeline indices, as warnings. Extra out-of-range waits become short pauses instead of crashing Manim.
- Visual/narration beat drift, such as a labeled visual matching the next narration sentence better than the sentence it is bound to.
- Visual clutter density, where sampled frames contain too many visible objects, labels, arrows, or old scene elements at once.

When a layout is wide, such as a solar system, graph, map, or timeline, build the full visual as a `VGroup` and call:

```python
system = VGroup(sun, *orbits, *planets, *labels)
fit_to_safe_frame(system)
```

Use `keep_in_safe_frame(label_or_panel)` after positioning floating text or callout panels.

Claude should not inject audio playback code into Manim by default. The cleaner split is: Claude writes the complete visual scene using full Manim capabilities, while the server reuses prepared audio, muxes it after render, and checks the result.

The installer preserves existing Claude Desktop config entries and adds an entry like:

```json
{
  "mcpServers": {
    "manim": {
      "type": "stdio",
      "command": "/path/to/python",
      "args": [
        "-m",
        "manim_mcp.server"
      ]
    }
  }
}
```

If you specifically want Claude Desktop to run from a source checkout through `uv`, use:

```bash
uv run manim-mcp-install-claude --source
```

## Native Dependencies

This project does not install system packages automatically. On macOS, Manim needs native tools such as Cairo, `pkg-config`, Pango, and FFmpeg for video output. Narration sync also needs `ffprobe` so the server can measure video/audio duration before muxing. LaTeX plus `dvisvgm` is optional unless scenes use `Tex` or `MathTex`.

The MCP server automatically prepends project-local TeX paths such as `.tinytex/bin/*` and `.texenv/bin` when checking tools and rendering scenes, so Claude Desktop does not need global TeX symlinks.

Run `check_environment` from Claude Desktop or:

```bash
uv run python -c "from manim_mcp.server import check_environment; import json; print(json.dumps(check_environment(), indent=2))"
```

## Package And Release

Build and validate the package locally:

```bash
uv sync --dev
uv run pytest
uv run python -m build
uv run twine check dist/*
```

The wheel contains only the `manim_mcp` Python package. Generated renders, local TeX installs, virtual environments, `.env`, and build outputs are excluded from source distributions.

The repository includes GitHub Actions for CI and release artifacts:

- `.github/workflows/ci.yml`: runs tests, builds the package, and checks distribution metadata.
- `.github/workflows/release.yml`: on a `v*` tag, builds `dist/*` and attaches the wheel/source archive to the GitHub release.

## Example Prompt For Claude

Ask Claude Desktop:

```text
Use the manim MCP server to render a low quality animation of a blue square rotating into a red circle.
```

With narration:

```text
Use the manim MCP server tool render_scene_with_narration to render a low quality explanation of sin theta. Use narration_sync_mode="timeline" and narration_audio_mode="segmented". Write the narration first: introduce the question, state what we are trying to understand, then explain step by step. Use tl = narration_timeline(self) and bind every sentence with tl.play_segment(...) or tl.wait_segment(...).
```

For a complex narrated video:

```text
Use prepare_narration first so you know exact sentence durations, then storyboard one visual beat per sentence and call render_scene_with_prepared_narration. Use full Manim capabilities, but keep each beat readable, fade or clear old VGroups between subtopics, and bind each narration segment to the matching visual beat with narration_timeline(self).
```

For more complex narrated scenes, use the MCP prompt `write_narrated_manim_scene`. It tells Claude to write the narration first, introduce the topic/problem before solving, storyboard readable visual beats, bind each sentence to `tl.play_segment(...)` or `tl.wait_segment(...)`, clear old groups between subtopics, and use `fit_to_safe_frame(...)` for large layouts.

Claude Desktop currently surfaces the returned links more reliably than inline MCP UI resources. By default this server does not embed `ui://` HTML or media bytes. It still creates a small local player page for the `Open player` link, but it does not show a separate redundant `preview.html` link in normal responses.

Every successful render now includes these visible access lines in the text result:

- `Open video`: read-only localhost MP4 stream for the current MCP server session.
- `Open player`: read-only localhost HTML player page.
- `Video path`: absolute local MP4 path.

The result metadata also includes `final_response_markdown` and `claude_response_instructions`. Claude should paste `final_response_markdown` into its chat response. If it forgets, ask Claude to call `get_render_access` with `job_id="latest"`.

By default, video bytes are not base64-embedded in tool responses because Claude Desktop rejects tool results larger than 1MB. For compatible MCP clients, you can opt in to `ui://` HTML resources with `include_ui_resource=true`, but local video/player links are the safer default.

If a rendered MP4 is silent, check its `metadata.json`. `narration_requested: false` means Claude called the plain render tool without narration text. Use `render_scene_with_narration` or explicitly pass `narration_text`. `narration_tts_backend` shows whether a render used `huggingface-api` or `local-kokoro`.
