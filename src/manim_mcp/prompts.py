"""LLM-facing copy: tool docstrings, Manim cheat sheet, scene skeleton, prompts.

Everything in this file is consumed by the LLM through MCP tool descriptions
or the ``write_narrated_manim_scene`` prompt template. We keep it in one
place so we can iterate on the wording without touching the wiring.

Design rules:

* Be concrete, not poetic.
* Start with a 1-line summary, then a "when to use" decision block,
  then a tight rules cheat sheet, then a runnable example skeleton.
* Repeat the most important rules across tools (LLMs scan, they don't read).
* Never bury the access-line instruction at the bottom -- mention it twice.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Manim CE quick reference -- prevents the most common LLM mistakes,
# especially the "Animation.__init__() got an unexpected keyword argument"
# failure mode. Kept short on purpose.
# ---------------------------------------------------------------------------

MANIM_API_CHEAT_SHEET = """
Manim CE quick reference (avoid common mistakes):

# Mobject vs Animation kwargs (this is the #1 source of TypeError):
- STYLE kwargs go on the MOBJECT: color=, fill_color=, fill_opacity=,
  stroke_color=, stroke_width=, stroke_opacity=, font=, font_size=, weight=.
- TIMING kwargs go on the ANIMATION: run_time=, rate_func=, lag_ratio=,
  reverse_rate_function=.
  WRONG: self.play(Write(eq, color=BLUE, font_size=48))
  RIGHT: eq = MathTex(r"\\frac{a}{b}", color=BLUE, font_size=48); self.play(Write(eq))

# Animations you can rely on:
  Create, Uncreate, Write, FadeIn, FadeOut, DrawBorderThenFill,
  GrowFromCenter, GrowFromEdge, GrowFromPoint, SpinInFromNothing,
  Transform, ReplacementTransform, TransformMatchingTex, TransformMatchingShapes,
  Indicate, Flash, Circumscribe, Wiggle, FocusOn, ApplyWave, ShowPassingFlash,
  AnimationGroup(a, b, lag_ratio=0.4), LaggedStart(*anims, lag_ratio=0.2)

# Text:
- Text("hello", font_size=42)                       # plain text, no LaTeX needed
- MarkupText("<b>bold</b> and <i>italic</i>")       # Pango markup
- MathTex(r"e^{i\\pi} + 1 = 0")                       # LaTeX (needs tex_ready)
- Tex(r"This requires LaTeX")                       # LaTeX
  -> Use Text/MarkupText unless the user explicitly needs LaTeX math/typesetting.

# Positioning + layout:
- thing.next_to(other, RIGHT, buff=0.3)
- thing.move_to(ORIGIN); thing.shift(UP * 1.5)
- thing.to_edge(LEFT, buff=0.5); thing.to_corner(UR)
- VGroup(a, b, c).arrange(RIGHT, buff=0.4)
- VGroup(a, b).arrange_in_grid(rows=2, buff=0.3)

# Camera moves:
- class MyScene(MovingCameraScene): self.play(self.camera.frame.animate.scale(0.5).move_to(target))
- class MyScene(ThreeDScene): self.set_camera_orientation(phi=70 * DEGREES, theta=-45 * DEGREES)

# Updaters / animated values:
- counter = ValueTracker(0)
- label = always_redraw(lambda: DecimalNumber(counter.get_value()).to_edge(UP))
- self.play(counter.animate.set_value(10), run_time=2)

# Cleanup between subtopics (very important for clean videos):
- self.play(FadeOut(prev_group))   # preferred
- self.remove(*prev_group)         # if you animated submobjects individually
- self.clear()                     # nuclear: clear everything before next beat
"""


# ---------------------------------------------------------------------------
# Scene skeleton -- shown in the prompt template + render docstrings.
# ---------------------------------------------------------------------------

SCENE_SKELETON = '''
from manim import *


class TopicExplainer(Scene):
    def construct(self):
        tl = narration_timeline(self)

        title = Text("Topic", font_size=56).to_edge(UP)
        tl.play_segment(0, FadeIn(title))             # sentence 0: introduce

        diagram = VGroup(
            Circle(radius=1.0, color=BLUE),
            Text("Idea", font_size=32),
        ).arrange(RIGHT, buff=0.4)
        fit_to_safe_frame(diagram)
        tl.play_segment(1, Create(diagram))           # sentence 1: state goal

        # ... bind every remaining narration sentence to one beat ...

        recap = Text("Key takeaway", font_size=42).next_to(diagram, DOWN, buff=0.6)
        tl.play_segment(N - 1, Write(recap))          # final sentence: summary
'''


# ---------------------------------------------------------------------------
# Authoring rules shared across every narrated render tool.
# ---------------------------------------------------------------------------

AUTHORING_CONTRACT = """
Authoring contract for narrated scenes:

NARRATION TEXT
- 5 to 9 short sentences. Target a 30-90 second video; never exceed 2 minutes.
- Sentence 0: hook / introduce the topic. Sentence 1: state what the viewer will learn.
  Middle sentences: one reasoning step each. Final sentence: summarize.
- Speak like a calm, friendly professor. Avoid filler words.
- Spell out short numbers ("five" not "5"). Avoid LaTeX, emojis, and special unicode in narration_text.
- Define every symbol the first time you use it.
- A blank line in narration_text becomes a longer pause (paragraph break).

VISUAL CONTRACT (one beat per sentence)
- Call `tl = narration_timeline(self)` once at the start of `construct()`.
- For each sentence i, call exactly one primary `tl.play_segment(i, animation_or_group)`
  or `tl.wait_segment(i)`. The visual must depict THAT sentence.
- The helper auto-inserts the planned post-segment silence; do not add your own
  `self.wait()` between segments unless you want extra pause.
- For abstract emphasis use `Indicate`, `Flash`, `Circumscribe`, `Wiggle`, `FocusOn`.

LAYOUT + READABILITY
- Default font_size for body text is 32-42; titles 48-64. Smaller is unreadable.
- Use `fit_to_safe_frame(group)` for wide layouts and `keep_in_safe_frame(label)`
  for floating labels. Both helpers are pre-injected; do not import them.
- Between subtopics: FadeOut the previous VGroup (or `self.clear()`) BEFORE
  building the next beat. Visual pile-ups are the most common quality failure.
- Stick to a small palette per video, e.g. BLUE_C, TEAL_C, YELLOW_C, GREY_B.

PACING
- Target 4-7 seconds per beat for explanations, 2-3s for transitions.
- If an animation needs MORE time than its sentence: lengthen that narration
  sentence, or merge two short sentences into one.
- If an animation needs LESS time: add a `tl.wait_segment(i)` right after to fill,
  or pass `hold=` to your `tl.play_segment(i, ..., hold=0.4)`.
- Animations forced below ~0.45s look like a "pop"; the helper enforces this floor.

WHAT NOT TO DO
- Do not redefine `narration_timeline`, `NarrationTimeline`, `fit_to_safe_frame`,
  or `keep_in_safe_frame`; the server injects them.
- Do not pass mobject style kwargs to animations (see Manim cheat sheet).
- Do not reuse comprehension variables (`i`, `p`) outside the comprehension.
- Do not chain timed `self.play` / `self.wait` outside `tl.play_segment` for
  important visual motion -- bind it to the segment that explains it.
"""


# ---------------------------------------------------------------------------
# Tool docstrings (kept here so tools.py stays readable)
# ---------------------------------------------------------------------------

PREPARE_NARRATION_DOC = """Prepare narration audio + per-sentence timings before writing the scene.

Recommended for any non-trivial educational video. Returns a
``prepared_narration_id`` and a ``timing_plan.segments`` list.

You MUST read every ``segments[i].text`` and ``segments[i].duration_seconds``
before writing scene code -- they are the contract for ``tl.play_segment(i, ...)``.
``segments[i].post_gap_seconds`` is the silence we'll insert after that
sentence; the injected helper handles it for you.

Then call ``render_scene_with_prepared_narration`` with the same
``prepared_narration_id``.
"""


RENDER_SCENE_DOC = (
    """Render a ManimCE scene and return media links + metadata.

When to use which tool:
  - Silent video                                 -> render_scene
  - Voice / narrated explanation, single call    -> render_scene_with_narration
  - Voice video with prepared narration + plan   -> render_scene_with_prepared_narration

Media bytes and preview HTML are NOT embedded by default to keep responses
small. MCP Apps-capable hosts may render the linked ``ui://`` player from
tool metadata, and the normal text response always includes
``Open video``, ``Open player``, and ``Video path`` lines -- include
``final_response_markdown`` verbatim in your reply so the user can open the video.

Args of note:
  narration_text          : optional spoken script. Without it, the video is silent.
  prepared_narration_id   : returned by prepare_narration; use this for higher quality.
  narration_sync_mode     : 'timeline' (default; per-sentence sync via the helper),
                            'fit' (global ffmpeg retime; capped),
                            'pad' (no retime; pad ends).
  narration_audio_mode    : 'segmented' (default; per-sentence TTS, REQUIRED for
                            sentence sync), 'single' (one TTS call; auto-upgraded
                            to 'segmented' if you also pick timeline).
  fail_on_quality_issues  : if true, severe sync/layout problems return as a
                            tool error so you can revise and rerender once.

After this tool returns, include ``final_response_markdown`` verbatim.
"""
    + AUTHORING_CONTRACT
    + "\n"
    + MANIM_API_CHEAT_SHEET
    + "\nExample skeleton:\n"
    + SCENE_SKELETON
)


RENDER_SCENE_WITH_NARRATION_DOC = (
    """Render a narrated ManimCE MP4 (one-shot: TTS + render + mux).

Use this when the user asks for voice / narration / a spoken explanation
and you do NOT need to plan timings beforehand. For a higher-quality flow,
call ``prepare_narration`` first then ``render_scene_with_prepared_narration``.

Required: ``narration_text``. Output is always MP4. Defaults to
``narration_sync_mode='timeline'`` and ``narration_audio_mode='segmented'``;
keep them unless you have a specific reason to change.

After this tool returns, include ``final_response_markdown`` verbatim
in your reply so the user can open the video.
"""
    + AUTHORING_CONTRACT
    + "\n"
    + MANIM_API_CHEAT_SHEET
    + "\nExample skeleton:\n"
    + SCENE_SKELETON
)


RENDER_SCENE_WITH_PREPARED_NARRATION_DOC = (
    """Render a narrated ManimCE MP4 using audio + timings from prepare_narration.

Use this for the highest-quality narrated videos. You should already have
read every segment of the prepared timing plan before writing code.

Required: ``prepared_narration_id`` from a successful ``prepare_narration``.
Output is always MP4. Defaults to ``narration_sync_mode='timeline'``.

After this tool returns, include ``final_response_markdown`` verbatim
in your reply so the user can open the video.
"""
    + AUTHORING_CONTRACT
    + "\n"
    + MANIM_API_CHEAT_SHEET
    + "\nExample skeleton:\n"
    + SCENE_SKELETON
)


GET_RENDER_ACCESS_DOC = """Return compact video / player links for a render job.

Use this when the user asks "where is the video?" or when you need to
re-fetch links for an earlier job. Pass ``job_id="latest"`` to grab the
most recent render. Include ``final_response_markdown`` verbatim in your reply.
"""


LIST_RENDERS_DOC = """List recent Manim MCP render jobs (newest first)."""


READ_RENDER_LOG_DOC = """Return bounded stdout/stderr logs for a render job.

Useful when ``render_scene*`` returned a generic error and you need the full
Manim traceback to diagnose what to fix in the scene code.
"""


# ---------------------------------------------------------------------------
# The @mcp.prompt template body.
# ---------------------------------------------------------------------------

def write_narrated_manim_scene_prompt_body(topic: str, quality: str = "low") -> str:
    return (
        f"""
Create and render a narrated ManimCE video about: {topic}

Recommended workflow:
- For complex explanations: call `prepare_narration` first to get measured
  per-sentence durations, plan one visual beat per sentence, then call
  `render_scene_with_prepared_narration`.
- For quick explanations: call `render_scene_with_narration` directly.
- Defaults: quality="{quality}", narration_sync_mode="timeline",
  narration_audio_mode="segmented", visual_quality_checks=true.
- Prefer `Text` and `MarkupText` unless the user explicitly asks for LaTeX
  math/typesetting. `Tex` and `MathTex` require a working local TeX toolchain.

Aim for a 30 to 90 second video; never exceed 2 minutes.
"""
        + AUTHORING_CONTRACT
        + "\n"
        + MANIM_API_CHEAT_SHEET
        + "\nExample skeleton:\n"
        + SCENE_SKELETON
        + (
            "\nIf the render or quality check fails, fix the reported diagnostic"
            " and rerender ONCE before responding to the user. On success,"
            " include `final_response_markdown` verbatim."
        )
    ).strip()
