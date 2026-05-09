# Changelog

## 0.2.0

### Major rewrite -- modular package, sharper LLM contract, better narration quality

The monolithic `server.py` (~5K lines) has been deprecated and replaced by 10
focused modules. Behavior preserved; quality, sync, and LLM guidance improved.

### Audio / narration

- **Real silence between sentences.** `concatenate_audio_segments` and
  `align_segmented_audio_to_timeline` now insert per-sentence silence
  (~0.18s sentences, ~0.32s after `?`/`!`, ~0.55s between paragraphs)
  instead of running TTS sentences back-to-back.
- **Paragraph-aware sentence splitting.** A blank line in `narration_text`
  is now a real beat boundary; short fragments no longer merge across
  paragraphs.
- **`tl.play_segment(...)` enforces minimum runtimes** (0.45s play, 0.20s
  wait) so animations can never collapse into a 0.05s "pop".
- **`hold` parameter is clamped** to fit the segment duration; a clamping
  warning is recorded for the quality report.
- **Per-segment audio overflow detection.** When a sentence's TTS audio
  outruns its rendered visual beat by more than 0.18s, we record an
  `audio_overflow_segment` quality issue.
- **Bounded ffmpeg retiming.** `mux_narration_audio` clamps the global
  `setpts` factor to `[0.65, 1.55]` and caps frame-clone extension at 6s,
  with quality errors when either limit is hit.
- **Auto-upgrade.** `narration_sync_mode='timeline'` with
  `narration_audio_mode='single'` now auto-upgrades to `segmented` and
  surfaces a warning, because segmented is required for true sentence sync.
- **Incremental timeline events.** `narration/timeline_actual.json` is
  written after every `tl.play_segment` / `tl.wait_segment` so partial
  renders still produce usable timing data.

### Visuals / quality checks

- **Robust background detection** (border-ring sample + median-per-channel
  + confidence score). Frames with low-confidence backgrounds are skipped
  instead of producing false-positive edge-touch errors.
- **Edge-touch is now a warning** (was an error); requires >=3 confident
  samples before reporting.
- **Visual clutter is now a warning** with thresholds tuned to top-level
  mobject count instead of glyph-rich submobject family count.
- **`fit_to_safe_frame`** gained a `recenter=` flag; **`keep_in_safe_frame`**
  scales down too-wide mobjects before nudging.

### Static analysis

- **Animation kwarg sanity check.** Catches the very common
  `Write(eq, color=BLUE)` style mistake that makes Manim raise
  `Animation.__init__() got an unexpected keyword argument`.
- **All Scene subclasses are now linted**, not just the one being rendered.
- **`if/else` is collapsed to the heavier branch** in the timed-call
  collector so we no longer reserve duplicate slots for unreachable code.
- **Unrolled loops also resolve `range(len(known_list))`** (in addition to
  literal `range(N)`).
- **Reserved-name renaming covers function/method parameters.**
- **Multi-line render error capture** so traces like the
  `Animation.__init__()` failure are returned with their context line.

### LLM contract

- **Sharp, sectioned tool docstrings.** Every render tool description now
  starts with a 1-line summary, a "when to use which tool" decision block,
  the authoring contract, the Manim API cheat sheet, and a runnable scene
  skeleton. The cheat sheet explicitly addresses the Animation kwarg pitfall.
- **Targeted at sub-2-minute videos.** Soft warning at >2400 chars,
  hard error at >4000 chars of `narration_text`.
- **`prepare_narration` usage instruction** now explicitly tells the LLM
  to read every `segments[i]` before writing scene code.
- **Removed `plan_narration_timing`** -- it produced fictional timings
  that misled timeline sync. Use `prepare_narration` instead.

### Internal

- New module layout: `config`, `safety`, `narration`, `scene_helpers`,
  `scene_prepare`, `render_io`, `quality`, `render_pipeline`, `prompts`,
  `tools`, plus a 30-line `server.py` entry point.
- `pathlib` is no longer in `BLOCKED_MODULES`; user scenes can load asset
  files via `Path(...)`.
- Tool path discovery is cached per process.
- Issues from `analyze_render_quality` are sorted (errors first, then by
  segment index, then by code) so the LLM sees the most actionable items first.

## 0.1.0

- Initial package release.
- Claude Desktop MCP server for rendering ManimCE scenes.
- Narrated MP4 rendering with hosted Hugging Face TTS or local Kokoro fallback.
- Inline `ui://` HTML preview resources and localhost media links.
- Timeline-aware narration sync with measured sentence durations.
- Basic safety preflight checks and render quality checks.
