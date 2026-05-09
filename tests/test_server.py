from __future__ import annotations

import json
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from manim_mcp import server


SIMPLE_SCENE = """
from manim import *

class Demo(Scene):
    def construct(self):
        square = Square()
        self.add(square)
"""


def pretend_ffmpeg_available(monkeypatch) -> None:
    """Keep mux unit tests independent of the runner's native ffmpeg install."""
    original_which = server.shutil.which

    def fake_which(command, path=None):
        if command == "ffmpeg":
            return "/fake/bin/ffmpeg"
        return original_which(command, path=path)

    monkeypatch.setattr(server.shutil, "which", fake_which)


def test_infer_scene_name_single_scene() -> None:
    assert server.infer_scene_name(SIMPLE_SCENE) == "Demo"


def test_infer_scene_name_requires_explicit_name_for_multiple_scenes() -> None:
    code = SIMPLE_SCENE + "\nclass Other(Scene):\n    def construct(self):\n        pass\n"
    with pytest.raises(ValueError, match="multiple Scene subclasses"):
        server.infer_scene_name(code)
    assert server.infer_scene_name(code, "Other") == "Other"


def test_safety_blocks_dangerous_imports_and_calls() -> None:
    violations = server.analyze_code_safety(
        """
from manim import *
import os

class Demo(Scene):
    def construct(self):
        open('/tmp/nope', 'w')
        eval('1 + 1')
"""
    )
    assert any("blocked import 'os'" in violation for violation in violations)
    assert any("blocked call 'open'" in violation for violation in violations)
    assert any("blocked call 'eval'" in violation for violation in violations)


def test_safety_does_not_block_manim_remove_method() -> None:
    violations = server.analyze_code_safety(
        """
from manim import *

class Demo(Scene):
    def construct(self):
        square = Square()
        self.add(square)
        self.remove(square)
"""
    )
    assert violations == []


def test_validation_flags_stale_comprehension_variable() -> None:
    violations = server.analyze_code_validation(
        """
from manim import *
import numpy as np

class Demo(Scene):
    def construct(self):
        points = [np.array([x, 0, 0]) for x in [1, 2]]
        dots = [Dot(p) for p in points]
        extra = Dot(p)
""",
        "Demo",
    )

    assert violations == [
        "line 9: name 'p' is not defined outside the comprehension that used it; "
        "assign from an explicit list element or loop over the list instead"
    ]


def test_extract_render_error_summary_from_manim_traceback() -> None:
    stderr = """
╭───────────────────── Traceback (most recent call last) ──────────────────────╮
│ /tmp/scene.py:12 in construct                                                │
╰──────────────────────────────────────────────────────────────────────────────╯
NameError: name 'p' is not defined
"""

    assert server.extract_render_error_summary(stderr) == "NameError: name 'p' is not defined"


def test_build_manim_command_uses_fixed_media_and_log_dirs(tmp_path) -> None:
    command = server.build_manim_command(
        script_path=tmp_path / "scene.py",
        scene_name="Demo",
        media_dir=tmp_path / "media",
        log_dir=tmp_path / "logs",
        quality="medium",
        output_format="png",
        python_executable="/tmp/python",
    )
    assert command[:4] == ["/tmp/python", "-m", "manim", "-qm"]
    assert "--media_dir" in command
    assert str(tmp_path / "media") in command
    assert "--log_dir" in command
    assert str(tmp_path / "logs") in command
    assert "--preview" not in command
    assert "-p" not in command
    assert "--save_last_frame" in command
    assert command[-2:] == [str(tmp_path / "scene.py"), "Demo"]


def test_discover_artifacts_returns_supported_media(tmp_path) -> None:
    media = tmp_path / "media"
    nested = media / "videos" / "scene" / "480p15"
    nested.mkdir(parents=True)
    artifact = nested / "Demo.mp4"
    artifact.write_bytes(b"video")
    partial_dir = nested / "partial_movie_files" / "Demo"
    partial_dir.mkdir(parents=True)
    (partial_dir / "uncached_00000.mp4").write_bytes(b"partial")
    (nested / "ignore.txt").write_text("nope", encoding="utf-8")

    artifacts = server.discover_artifacts(media)
    assert len(artifacts) == 1
    assert artifacts[0]["path"] == str(artifact.resolve())
    assert artifacts[0]["format"] == "mp4"
    assert artifacts[0]["size_bytes"] == 5


def test_render_asset_url_serves_render_root_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "RENDER_ROOT", tmp_path)
    artifact = tmp_path / "20260505T000000Z-deadbeef" / "media" / "Demo.mp4"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"abcdef")

    url = server.render_asset_url(artifact)

    assert url is not None
    assert url.startswith("http://127.0.0.1:")
    request = Request(url, headers={"Range": "bytes=1-3"})
    with urlopen(request, timeout=5) as response:
        assert response.status == 206
        assert response.read() == b"bcd"


def test_render_scene_blocks_unsafe_code_before_creating_job(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "RENDER_ROOT", tmp_path / "renders")
    result = server.render_scene("import os\n")
    assert result.isError is True
    assert result.structuredContent["success"] is False
    assert result.structuredContent["blocked"] is True
    assert not (tmp_path / "renders").exists()


def test_check_environment_uses_path_uv_and_does_not_require_it(monkeypatch) -> None:
    commands = []

    def fake_run_probe(command, timeout=5):
        commands.append(command)
        return {
            "available": command[0] != "uv",
            "command": command,
            "path": command[0] if command[0] != "uv" else None,
        }

    monkeypatch.setattr(server, "_run_probe", fake_run_probe)
    monkeypatch.setattr(server, "_version_for_distribution", lambda name: "1.0.0")
    monkeypatch.setattr(server, "_env_value", lambda name: None)

    result = server.check_environment()

    assert ["uv", "--version"] in commands
    assert not any(str(command[0]).endswith("/.local/bin/uv") for command in commands)
    assert result["checks"]["uv"]["available"] is False
    assert "uv" not in result["missing_required"]
    assert result["ok"] is True
    assert any("uv is optional" in note for note in result["notes"])


def test_render_scene_allows_narration_without_hf_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "RENDER_ROOT", tmp_path / "renders")
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("HF_TOKEN", raising=False)

    def fake_segmented(text, narration_dir, *, model, provider, timeout_seconds, concat_timeout_seconds):
        audio_path = narration_dir / "narration.wav"
        audio_path.write_bytes(b"audio")
        return (
            {
                "path": str(audio_path.resolve()),
                "uri": audio_path.resolve().as_uri(),
                "model": model,
                "provider": server.LOCAL_NARRATION_PROVIDER,
                "backend": server.LOCAL_NARRATION_PROVIDER,
                "audio_mode": "segmented",
                "size_bytes": 5,
                "duration_seconds": 1.0,
                "audio_stream_count": 1,
                "segment_count": 1,
                "segments": [],
            },
            {
                "segment_count": 1,
                "word_count": 2,
                "target_total_seconds": 1.0,
                "timing_source": "measured_tts_segments",
                "segments": [{"index": 0, "text": "A short narration.", "duration_seconds": 1.0}],
            },
        )

    def fake_render(command, **kwargs):
        media_dir = Path(command[command.index("--media_dir") + 1])
        output = media_dir / "videos" / "scene" / "480p15" / "Demo.mp4"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"video")
        return server.subprocess.CompletedProcess(command, 0, "rendered", "")

    def fake_mux(video_path, audio_path, output_path, *, sync_mode, timeout_seconds):
        output_path.write_bytes(b"narrated")
        return {
            "path": str(output_path.resolve()),
            "uri": output_path.resolve().as_uri(),
            "mime_type": "video/mp4",
            "size_bytes": output_path.stat().st_size,
            "mode": "mux_audio",
            "requested_sync_mode": sync_mode,
            "output_audio_stream_count": 1,
        }

    monkeypatch.setattr(server, "synthesize_segmented_narration_audio", fake_segmented)
    monkeypatch.setattr(server.subprocess, "run", fake_render)
    monkeypatch.setattr(server, "mux_narration_audio", fake_mux)

    result = server.render_scene(
        SIMPLE_SCENE,
        narration_text="A short narration.",
        visual_quality_checks=False,
    )

    assert result.isError is False, result.structuredContent.get("error")
    assert result.structuredContent["narration_tts_backend"] == server.LOCAL_NARRATION_PROVIDER
    assert result.structuredContent["narration_audio"]["backend"] == server.LOCAL_NARRATION_PROVIDER


def test_build_narration_timing_plan_scales_segments_to_audio_duration() -> None:
    plan = server.build_narration_timing_plan(
        "First, draw the triangle. Then label the opposite side. Finally, show the ratio.",
        total_duration_seconds=9.0,
    )

    assert plan["segment_count"] == 3
    assert plan["target_total_seconds"] == 9.0
    assert plan["segments"][0]["start_seconds"] == 0.0
    assert plan["segments"][-1]["end_seconds"] == 9.0
    assert sum(segment["duration_seconds"] for segment in plan["segments"]) == pytest.approx(9.0)


def test_build_measured_narration_timing_plan_uses_exact_segment_durations() -> None:
    plan = server.build_measured_narration_timing_plan(
        ["Show Earth.", "Move the Moon around it.", "Reveal tidal locking."],
        [1.2, 3.4, 2.1],
    )

    assert plan["timing_source"] == "measured_tts_segments"
    assert plan["target_total_seconds"] == 6.7
    assert [segment["duration_seconds"] for segment in plan["segments"]] == [1.2, 3.4, 2.1]
    assert [segment["start_seconds"] for segment in plan["segments"]] == [0.0, 1.2, 4.6]
    assert plan["segments"][-1]["end_seconds"] == 6.7


def test_prepare_narrated_scene_code_retimes_play_and_wait_calls() -> None:
    plan = {
        "segments": [
            {"duration_seconds": 2.0},
            {"duration_seconds": 3.0},
        ]
    }
    code, report = server.prepare_narrated_scene_code(
        """
from manim import *

class Demo(Scene):
    def construct(self):
        square = Square()
        self.play(Create(square))
        self.wait(1)
""",
        scene_name="Demo",
        timing_plan=plan,
        sync_mode="timeline",
    )

    assert report["scene_retimed"] is True
    assert report["timed_call_count"] == 2
    assert "NARRATION_TIMING" in code
    assert "MANIM_MCP_CALL_DURATIONS = [2.0, 3.0]" in code
    assert "run_time=_manim_mcp_next_duration(1.0)" in code
    assert "self.wait(_manim_mcp_next_duration(1.0))" in code


def test_prepare_narrated_scene_code_preserves_future_import_position() -> None:
    code, _ = server.prepare_narrated_scene_code(
        '"""module docs"""\nfrom __future__ import annotations\nfrom manim import *\n\nclass Demo(Scene):\n    def construct(self):\n        self.wait()\n',
        scene_name="Demo",
        timing_plan={"segments": [{"duration_seconds": 1.0}]},
        sync_mode="fit",
    )

    assert code.startswith('"""module docs"""\nfrom __future__ import annotations\nNARRATION_TIMING')


def test_prepare_narrated_scene_code_skips_dynamic_loop_repeats() -> None:
    plan = {"segments": [{"duration_seconds": 6.0}]}
    code, report = server.prepare_narrated_scene_code(
        """
from manim import *

class Demo(Scene):
    def construct(self):
        dots = VGroup(*[Dot() for _ in range(3)])
        for dot in dots:
            self.play(FadeIn(dot))
""",
        scene_name="Demo",
        timing_plan=plan,
        sync_mode="timeline",
    )

    assert report["scene_retimed"] is False
    assert report["timed_call_count"] == 0
    assert report["dynamic_loop_timed_call_count"] == 1
    assert "run_time=6.0" not in code


def test_prepare_narrated_scene_code_divides_static_range_loop_duration() -> None:
    plan = {"segments": [{"duration_seconds": 6.0}]}
    code, report = server.prepare_narrated_scene_code(
        """
from manim import *

class Demo(Scene):
    def construct(self):
        for i in range(3):
            self.play(FadeIn(Dot()))
""",
        scene_name="Demo",
        timing_plan=plan,
        sync_mode="timeline",
    )

    assert report["scene_retimed"] is True
    assert report["timed_call_count"] == 3
    assert report["static_loop_timed_call_count"] == 3
    assert report["call_durations_seconds"] == [2.0, 2.0, 2.0]
    assert "MANIM_MCP_CALL_DURATIONS = [2.0, 2.0, 2.0]" in code
    assert "run_time=_manim_mcp_next_duration(1.0)" in code


def test_prepare_narrated_scene_code_retimes_named_static_list_loop() -> None:
    plan = {"segments": [{"duration_seconds": 3.0}]}
    code, report = server.prepare_narrated_scene_code(
        """
from manim import *

class Demo(Scene):
    def construct(self):
        items = ["Mercury", "Venus", "Earth"]
        for name in items:
            self.play(Write(Text(name)))
""",
        scene_name="Demo",
        timing_plan=plan,
        sync_mode="timeline",
    )

    assert report["scene_retimed"] is True
    assert report["dynamic_loop_timed_call_count"] == 0
    assert report["static_loop_timed_call_count"] == 3
    assert report["call_durations_seconds"] == [1.0, 1.0, 1.0]
    assert "MANIM_MCP_CALL_DURATIONS = [1.0, 1.0, 1.0]" in code


def test_prepare_narrated_scene_code_reports_explicit_timeline_usage() -> None:
    code, report = server.prepare_narrated_scene_code(
        """
from manim import *

class Demo(Scene):
    def construct(self):
        tl = narration_timeline(self)
        tl.play_segment(0, FadeIn(Square()))
""",
        scene_name="Demo",
        timing_plan={"segments": [{"duration_seconds": 2.0}]},
        sync_mode="timeline",
    )

    assert report["explicit_timeline_used"] is True
    assert report["explicit_timeline_call_count"] == 2
    assert report["explicit_timeline_segment_indices"] == [0]
    assert report["explicit_timeline_missing_segments"] == []
    assert report["scene_retimed"] is False
    assert "no automatic self.play/self.wait retiming was needed" in report["warning"]
    assert "NARRATION_TIMING" in code


def test_prepare_narrated_scene_code_reports_missing_explicit_timeline_segments() -> None:
    code, report = server.prepare_narrated_scene_code(
        """
from manim import *

class Demo(Scene):
    def construct(self):
        tl = narration_timeline(self)
        tl.play_segment(0, FadeIn(Square()))
""",
        scene_name="Demo",
        timing_plan={
            "segments": [
                {"duration_seconds": 1.0},
                {"duration_seconds": 1.0},
                {"duration_seconds": 1.0},
            ]
        },
        sync_mode="timeline",
    )

    assert report["explicit_timeline_used"] is True
    assert report["explicit_timeline_segment_count"] == 3
    assert report["explicit_timeline_covered_segment_count"] == 1
    assert report["explicit_timeline_missing_segments"] == [1, 2]
    assert report["explicit_timeline_coverage_ratio"] == pytest.approx(0.333)
    assert "NARRATION_TIMING" in code


def test_prepare_narrated_scene_code_reports_out_of_range_timeline_segments() -> None:
    code, report = server.prepare_narrated_scene_code(
        """
from manim import *

class Demo(Scene):
    def construct(self):
        tl = narration_timeline(self)
        tl.play_segment(0, FadeIn(Square()))
        tl.wait_segment(1)
""",
        scene_name="Demo",
        timing_plan={"segments": [{"duration_seconds": 1.0}]},
        sync_mode="timeline",
    )

    assert report["explicit_timeline_segment_indices"] == [0]
    assert report["explicit_timeline_out_of_range_segments"] == [1]
    assert 'return dict(index=index, text="", duration_seconds=0.05, out_of_range=True)' in code


def test_narration_timeline_helper_treats_extra_segment_as_short_pause() -> None:
    namespace = {
        "config": type("Config", (), {"frame_width": 14.2, "frame_height": 8.0})(),
        "RIGHT": 1,
        "LEFT": 1,
        "UP": 1,
        "DOWN": 1,
    }
    exec(
        server.narration_timing_helper_source(
            {"segments": [{"duration_seconds": 1.0}]},
        ),
        namespace,
    )

    class FakeScene:
        waits = []

        def wait(self, duration):
            self.waits.append(duration)

    fake_scene = FakeScene()
    timeline = namespace["narration_timeline"](fake_scene)
    timeline.wait_segment(1)

    assert fake_scene.waits == [0.05]


def test_prepare_narrated_scene_code_protects_injected_timeline_helper() -> None:
    code, report = server.prepare_narrated_scene_code(
        """
from manim import *

def narration_timeline(scene):
    return None

class Demo(Scene):
    def construct(self):
        tl = narration_timeline(self)
        tl.play_segment(0, FadeIn(Square()))
        self.wait(1)
""",
        scene_name="Demo",
        timing_plan={"segments": [{"duration_seconds": 2.0}]},
        sync_mode="timeline",
    )

    assert report["reserved_helper_name_renames"][0]["from"] == "narration_timeline"
    assert report["reserved_helper_name_renames"][0]["to"] == "_manim_mcp_user_narration_timeline"
    assert report["scene_retimed"] is False
    assert report["timed_call_count"] == 1
    assert report["outside_timeline_timed_call_count"] == 1
    assert code.count("def narration_timeline(scene):") == 1
    assert "def _manim_mcp_user_narration_timeline(scene):" in code
    assert "MANIM_MCP_CALL_DURATIONS = []" in code
    assert "self.wait(1)" in code
    assert "self.wait(_manim_mcp_next_duration" not in code


def test_prepare_narrated_scene_code_protects_local_timeline_helper() -> None:
    code, report = server.prepare_narrated_scene_code(
        """
from manim import *

class Demo(Scene):
    def construct(self):
        def narration_timeline(scene):
            return None
        tl = narration_timeline(self)
        tl.play_segment(0, FadeIn(Square()))
""",
        scene_name="Demo",
        timing_plan={"segments": [{"duration_seconds": 2.0}]},
        sync_mode="timeline",
    )

    assert report["reserved_helper_name_renames"][0]["kind"] == "LocalFunctionDef"
    assert code.count("def narration_timeline(scene):") == 1
    assert "def _manim_mcp_user_narration_timeline(scene):" in code


def test_write_narrated_manim_scene_prompt_guides_sync_and_frame_safety() -> None:
    prompt = server.write_narrated_manim_scene_prompt("solar system", quality="medium")

    assert "render_scene_with_narration" in prompt
    assert 'quality="medium"' in prompt
    assert "tl.play_segment" in prompt
    assert "fit_to_safe_frame" in prompt
    assert "fail_on_quality_issues=true" in prompt
    assert "final_response_markdown" in prompt
    assert "introduces the topic/problem" in prompt
    assert "step by step" in prompt
    assert "Use ManimCE normally" in prompt
    assert "Avoid common Python mistakes" in prompt
    assert "fix the reported diagnostic" in prompt


def test_synthesize_narration_audio_uses_local_kokoro_without_hf_token(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)

    def fake_local(text, output_path, *, model, timeout_seconds):
        output_path.write_bytes(b"audio")
        return {
            "path": str(output_path.resolve()),
            "uri": output_path.resolve().as_uri(),
            "model": model,
            "provider": server.LOCAL_NARRATION_PROVIDER,
            "backend": server.LOCAL_NARRATION_PROVIDER,
            "audio_mode": "single",
            "size_bytes": output_path.stat().st_size,
            "duration_seconds": 1.0,
            "audio_stream_count": 1,
        }

    monkeypatch.setattr(server, "synthesize_local_kokoro_audio", fake_local)

    result = server.synthesize_narration_audio("hello", tmp_path / "narration.wav")

    assert result["backend"] == server.LOCAL_NARRATION_PROVIDER
    assert result["provider"] == server.LOCAL_NARRATION_PROVIDER


def test_synthesize_narration_audio_retries_and_verifies_stream(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "test-token")
    calls = {"count": 0}

    class FakeClient:
        def __init__(self, *, provider, api_key, timeout):
            assert provider == server.DEFAULT_NARRATION_PROVIDER
            assert api_key == "test-token"
            assert timeout == 120

        def text_to_speech(self, text, *, model):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("temporary outage")
            assert text == "hello"
            assert model == server.DEFAULT_NARRATION_MODEL
            return b"audio"

    monkeypatch.setattr(server.time, "sleep", lambda seconds: None)
    monkeypatch.setitem(
        __import__("sys").modules,
        "huggingface_hub",
        type("FakeModule", (), {"InferenceClient": FakeClient}),
    )
    monkeypatch.setattr(server, "probe_media_duration", lambda path: 1.25)
    monkeypatch.setattr(server, "probe_media_streams", lambda path: [{"codec_type": "audio"}])

    result = server.synthesize_narration_audio("hello", tmp_path / "narration.wav")

    assert calls["count"] == 2
    assert result["duration_seconds"] == 1.25
    assert result["audio_stream_count"] == 1
    assert result["backend"] == "huggingface-api"


def test_synthesize_local_kokoro_audio_writes_chunks(tmp_path, monkeypatch) -> None:
    class FakePipeline:
        def __call__(self, text, *, voice):
            assert text == "hello"
            assert voice == server.DEFAULT_LOCAL_NARRATION_VOICE
            yield "hello", "HH AH L OW", [0.1, 0.2]
            yield "hello", "HH AH L OW", [0.3]

    class FakeSoundFile:
        writes = []

        @staticmethod
        def write(path, audio, sample_rate):
            FakeSoundFile.writes.append((path, audio, sample_rate))
            Path(path).write_bytes(b"wav")

    monkeypatch.setattr(server, "_local_kokoro_pipeline", lambda lang_code, repo_id: FakePipeline())
    monkeypatch.setitem(__import__("sys").modules, "soundfile", FakeSoundFile)
    monkeypatch.setattr(server, "probe_media_duration", lambda path: 0.25)
    monkeypatch.setattr(server, "probe_media_streams", lambda path: [{"codec_type": "audio"}])

    result = server.synthesize_local_kokoro_audio("hello", tmp_path / "local.wav")

    assert result["backend"] == server.LOCAL_NARRATION_PROVIDER
    assert result["voice"] == server.DEFAULT_LOCAL_NARRATION_VOICE
    assert result["sample_rate"] == 24000
    assert result["chunk_count"] == 2
    assert FakeSoundFile.writes[0][2] == 24000


def test_synthesize_segmented_narration_audio_uses_real_segment_durations(tmp_path, monkeypatch) -> None:
    durations = {
        "First, show Earth.": 1.4,
        "Then move the Moon.": 2.6,
        "Finally show tidal locking.": 3.2,
    }

    def fake_synthesize(text, output_path, *, model, provider, timeout_seconds):
        output_path.write_bytes(text.encode("utf-8"))
        duration = durations[text]
        return {
            "path": str(output_path.resolve()),
            "uri": output_path.resolve().as_uri(),
            "model": model,
            "provider": provider,
            "audio_mode": "single",
            "size_bytes": output_path.stat().st_size,
            "duration_seconds": duration,
            "audio_stream_count": 1,
        }

    def fake_concat(segment_paths, output_path, *, timeout_seconds):
        output_path.write_bytes(b"joined")
        return {
            "path": str(output_path.resolve()),
            "uri": output_path.resolve().as_uri(),
            "size_bytes": output_path.stat().st_size,
            "duration_seconds": sum(durations.values()),
            "audio_stream_count": 1,
            "command": ["ffmpeg", "concat"],
            "stdout_tail": "",
            "stderr_tail": "",
        }

    monkeypatch.setattr(server, "synthesize_narration_audio", fake_synthesize)
    monkeypatch.setattr(server, "concatenate_audio_segments", fake_concat)

    audio, plan = server.synthesize_segmented_narration_audio(
        "First, show Earth. Then move the Moon. Finally show tidal locking.",
        tmp_path / "narration",
    )

    assert audio["audio_mode"] == "segmented"
    assert audio["segment_count"] == 3
    assert audio["duration_seconds"] == pytest.approx(7.2)
    assert [segment["duration_seconds"] for segment in audio["segments"]] == [1.4, 2.6, 3.2]
    assert plan["timing_source"] == "measured_tts_segments"
    assert [segment["duration_seconds"] for segment in plan["segments"]] == [1.4, 2.6, 3.2]


def test_env_value_reads_project_dotenv(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".env").write_text("HF_TOKEN='test-token'\n", encoding="utf-8")
    assert server._env_value("HF_TOKEN") == "test-token"


def test_add_narration_updates_artifacts(tmp_path, monkeypatch) -> None:
    job_dir = tmp_path / "job"
    media_dir = job_dir / "media"
    output = media_dir / "videos" / "scene" / "480p15" / "Demo.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"video")
    metadata = {
        "success": True,
        "job_id": "20260505T000000Z-deadbeef",
        "scene_name": "Demo",
        "job_dir": str(job_dir),
        "media_dir": str(media_dir),
        "artifacts": server.discover_artifacts(media_dir),
    }

    def fake_synthesize(text, output_path, *, model, provider, timeout_seconds):
        output_path.write_bytes(b"audio")
        return {
            "path": str(output_path),
            "uri": output_path.resolve().as_uri(),
            "model": model,
            "provider": provider,
            "size_bytes": 5,
        }

    def fake_mux(video_path, audio_path, output_path, *, sync_mode, timeout_seconds):
        output_path.write_bytes(b"narrated video")
        return {
            "path": str(output_path),
            "uri": output_path.resolve().as_uri(),
            "mime_type": "video/mp4",
            "size_bytes": output_path.stat().st_size,
            "mode": "mux_audio",
            "requested_sync_mode": sync_mode,
        }

    monkeypatch.setattr(server, "synthesize_narration_audio", fake_synthesize)
    monkeypatch.setattr(server, "mux_narration_audio", fake_mux)

    server.add_narration_to_render(metadata, "Narrate this.")

    assert metadata["narration"]["text"] == "Narrate this."
    assert metadata["narration"]["audio"]["model"] == server.DEFAULT_NARRATION_MODEL
    assert metadata["narration"]["video"]["path"].endswith("Demo_narrated.mp4")
    assert metadata["narration"]["video"]["requested_sync_mode"] == "fit"
    assert any(artifact["path"].endswith("Demo_narrated.mp4") for artifact in metadata["artifacts"])


def test_render_scene_metadata_uses_segmented_audio_before_render(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "RENDER_ROOT", tmp_path / "renders")
    monkeypatch.setenv("HF_TOKEN", "test-token")

    def fake_segmented(text, narration_dir, *, model, provider, timeout_seconds, concat_timeout_seconds):
        audio_path = narration_dir / "narration.wav"
        audio_path.write_bytes(b"audio")
        return (
            {
                "path": str(audio_path.resolve()),
                "uri": audio_path.resolve().as_uri(),
                "model": model,
                "provider": provider,
                "audio_mode": "segmented",
                "size_bytes": 5,
                "duration_seconds": 4.0,
                "audio_stream_count": 1,
                "segment_count": 2,
                "segments": [],
            },
            {
                "segment_count": 2,
                "word_count": 4,
                "target_total_seconds": 4.0,
                "timing_source": "measured_tts_segments",
                "segments": [
                    {"index": 0, "text": "First.", "duration_seconds": 2.0},
                    {"index": 1, "text": "Second.", "duration_seconds": 2.0},
                ],
            },
        )

    def fake_render(command, **kwargs):
        media_dir = Path(command[command.index("--media_dir") + 1])
        output = media_dir / "videos" / "scene" / "480p15" / "Demo.mp4"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"video")
        return server.subprocess.CompletedProcess(command, 0, "rendered", "")

    def fake_mux(video_path, audio_path, output_path, *, sync_mode, timeout_seconds):
        assert audio_path.name == "narration.wav"
        assert sync_mode == "timeline"
        output_path.write_bytes(b"narrated")
        return {
            "path": str(output_path.resolve()),
            "uri": output_path.resolve().as_uri(),
            "mime_type": "video/mp4",
            "size_bytes": output_path.stat().st_size,
            "mode": "mux_audio",
            "requested_sync_mode": sync_mode,
            "output_audio_stream_count": 1,
        }

    monkeypatch.setattr(server, "synthesize_segmented_narration_audio", fake_segmented)
    monkeypatch.setattr(server.subprocess, "run", fake_render)
    monkeypatch.setattr(server, "mux_narration_audio", fake_mux)

    timed_scene = """
from manim import *

class Demo(Scene):
    def construct(self):
        square = Square()
        self.play(Create(square))
        self.wait(1)
"""
    metadata = server._render_scene_metadata(
        timed_scene,
        narration_text="First. Second.",
        narration_sync_mode="timeline",
        visual_quality_checks=False,
    )

    assert metadata["success"] is True, metadata.get("error")
    assert metadata["narration_audio_mode"] == "segmented"
    assert metadata["narration_audio"]["audio_mode"] == "segmented"
    assert metadata["narration_timing_plan"]["timing_source"] == "measured_tts_segments"
    assert metadata["narration_sync"]["scene_retimed"] is True
    assert "NARRATION_TIMING" in Path(metadata["script_path"]).read_text(encoding="utf-8")
    assert metadata["artifacts"][0]["path"].endswith("Demo_narrated.mp4")


def test_analyze_render_quality_flags_dynamic_loop_and_global_retime(tmp_path, monkeypatch) -> None:
    video = tmp_path / "Demo.mp4"
    video.write_bytes(b"video")
    metadata = {
        "success": True,
        "narration_requested": True,
        "narration_sync": {
            "dynamic_loop_timed_call_count": 2,
            "explicit_timeline_used": False,
        },
        "narration": {
            "video": {
                "mode": "fit_video_to_audio",
                "video_duration_seconds": 82.0,
                "audio_duration_seconds": 58.0,
            }
        },
        "artifacts": [
            {
                "path": str(video.resolve()),
                "uri": video.resolve().as_uri(),
                "format": "mp4",
                "mime_type": "video/mp4",
                "size_bytes": video.stat().st_size,
            }
        ],
    }
    monkeypatch.setattr(
        server,
        "analyze_video_frame_bounds",
        lambda path: {"ok": True, "edge_touch_count": 0, "content_sample_count": 3},
    )

    quality = server.analyze_render_quality(metadata)

    assert quality["ok"] is False
    assert {issue["code"] for issue in quality["issues"]} == {
        "dynamic_loop_timing",
        "severe_global_retime",
    }


def test_analyze_render_quality_flags_incomplete_timeline_coverage(tmp_path, monkeypatch) -> None:
    video = tmp_path / "Demo.mp4"
    video.write_bytes(b"video")
    metadata = {
        "success": True,
        "narration_requested": True,
        "narration_sync": {
            "explicit_timeline_used": True,
            "explicit_timeline_segment_count": 4,
            "explicit_timeline_covered_segment_count": 2,
            "explicit_timeline_missing_segments": [2, 3],
            "explicit_timeline_dynamic_segment_call_count": 0,
        },
        "artifacts": [
            {
                "path": str(video.resolve()),
                "uri": video.resolve().as_uri(),
                "format": "mp4",
                "mime_type": "video/mp4",
                "size_bytes": video.stat().st_size,
            }
        ],
    }
    monkeypatch.setattr(
        server,
        "analyze_video_frame_bounds",
        lambda path: {"ok": True, "edge_touch_count": 0, "content_sample_count": 3},
    )

    quality = server.analyze_render_quality(metadata)

    assert quality["ok"] is False
    assert quality["issues"][0]["code"] == "incomplete_timeline_coverage"
    assert quality["issues"][0]["missing_segments"] == [2, 3]


def test_analyze_render_quality_flags_timeline_duration_mismatch() -> None:
    metadata = {
        "success": True,
        "narration_requested": True,
        "narration_sync": {"explicit_timeline_used": True},
        "narration": {
            "video": {
                "mode": "fit_video_to_audio",
                "audio_duration_seconds": 60.0,
                "video_duration_seconds": 90.0,
            }
        },
        "artifacts": [],
    }

    quality = server.analyze_render_quality(metadata)

    assert quality["ok"] is False
    assert quality["issues"][0]["code"] == "severe_timeline_duration_mismatch"


def test_analyze_render_quality_warns_for_out_of_range_timeline_segment() -> None:
    metadata = {
        "success": True,
        "narration_requested": True,
        "narration_sync": {
            "explicit_timeline_used": True,
            "explicit_timeline_segment_count": 1,
            "explicit_timeline_covered_segment_count": 1,
            "explicit_timeline_out_of_range_segments": [1],
        },
        "artifacts": [],
    }

    quality = server.analyze_render_quality(metadata)

    assert quality["ok"] is True
    assert quality["issues"][0]["severity"] == "warning"
    assert quality["issues"][0]["code"] == "out_of_range_timeline_segment"


def test_analyze_render_quality_flags_frame_edge_contact(tmp_path, monkeypatch) -> None:
    video = tmp_path / "Demo.mp4"
    video.write_bytes(b"video")
    metadata = {
        "success": True,
        "narration_requested": False,
        "artifacts": [
            {
                "path": str(video.resolve()),
                "uri": video.resolve().as_uri(),
                "format": "mp4",
                "mime_type": "video/mp4",
                "size_bytes": video.stat().st_size,
            }
        ],
    }
    monkeypatch.setattr(
        server,
        "analyze_video_frame_bounds",
        lambda path: {
            "ok": False,
            "edge_touch_count": 4,
            "content_sample_count": 6,
            "edge_hit_ratio": 0.667,
        },
    )

    quality = server.analyze_render_quality(metadata)

    assert quality["ok"] is False
    assert quality["issues"][0]["code"] == "content_touches_frame_edge"
    assert metadata["visual_bounds"]["edge_touch_count"] == 4


def test_render_scene_tool_result_includes_links_for_quality_failed_artifacts(tmp_path, monkeypatch) -> None:
    render_root = tmp_path / "renders"
    monkeypatch.setattr(server, "RENDER_ROOT", render_root)
    job_dir = render_root / "20260505T000000Z-deadbeef"
    media_dir = job_dir / "media"
    output = media_dir / "videos" / "scene" / "480p15" / "Demo.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"video")
    metadata = {
        "success": False,
        "quality_failed": True,
        "error": "Render completed, but quality checks failed.",
        "job_id": "20260505T000000Z-deadbeef",
        "scene_name": "Demo",
        "job_dir": str(job_dir),
        "artifacts": server.discover_artifacts(media_dir),
    }
    server.create_preview_html(metadata)

    result = server._render_scene_tool_result(metadata)

    assert result.isError is True
    assert "Open video: http://127.0.0.1:" in result.content[0].text
    assert "Video path:" in result.content[0].text
    assert "MANIM RENDER ACCESS" in result.content[0].text
    assert "final_response_markdown" in result.structuredContent
    assert any(item.type == "resource_link" and item.mimeType == "video/mp4" for item in result.content)


def test_mux_narration_pads_shorter_audio_to_video_duration(tmp_path, monkeypatch) -> None:
    pretend_ffmpeg_available(monkeypatch)
    video_path = tmp_path / "video.mp4"
    audio_path = tmp_path / "audio.wav"
    output_path = tmp_path / "narrated.mp4"
    video_path.write_bytes(b"video")
    audio_path.write_bytes(b"audio")

    def fake_duration(path):
        if path == video_path:
            return 10.0
        if path == audio_path:
            return 3.0
        if path == output_path:
            return 10.0
        return None

    def fake_run(command, **kwargs):
        output_path.write_bytes(b"muxed")
        return server.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(server, "probe_media_duration", fake_duration)
    monkeypatch.setattr(server, "probe_media_streams", lambda path: [{"codec_type": "audio"}])
    monkeypatch.setattr(server.subprocess, "run", fake_run)

    result = server.mux_narration_audio(video_path, audio_path, output_path, sync_mode="pad")

    assert result["mode"] == "pad_audio"
    assert result["output_audio_stream_count"] == 1
    assert "apad=whole_dur=10.000" in " ".join(result["command"])


def test_mux_narration_fits_video_to_audio_by_default(tmp_path, monkeypatch) -> None:
    pretend_ffmpeg_available(monkeypatch)
    video_path = tmp_path / "video.mp4"
    audio_path = tmp_path / "audio.wav"
    output_path = tmp_path / "narrated.mp4"
    video_path.write_bytes(b"video")
    audio_path.write_bytes(b"audio")

    def fake_duration(path):
        if path == video_path:
            return 2.0
        if path == audio_path:
            return 5.0
        if path == output_path:
            return 5.0
        return None

    def fake_run(command, **kwargs):
        output_path.write_bytes(b"muxed")
        return server.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(server, "probe_media_duration", fake_duration)
    monkeypatch.setattr(server, "probe_media_streams", lambda path: [{"codec_type": "audio"}])
    monkeypatch.setattr(server.subprocess, "run", fake_run)

    result = server.mux_narration_audio(video_path, audio_path, output_path)

    assert result["mode"] == "fit_video_to_audio"
    assert result["duration_delta_seconds"] == 3.0
    assert "setpts=2.50000000*PTS" in " ".join(result["command"])


def test_mux_narration_requires_duration_measurements_for_sync(tmp_path, monkeypatch) -> None:
    pretend_ffmpeg_available(monkeypatch)
    video_path = tmp_path / "video.mp4"
    audio_path = tmp_path / "audio.wav"
    output_path = tmp_path / "narrated.mp4"
    video_path.write_bytes(b"video")
    audio_path.write_bytes(b"audio")

    monkeypatch.setattr(server, "probe_media_duration", lambda path: None)

    with pytest.raises(ValueError, match="Narration sync requires ffprobe"):
        server.mux_narration_audio(video_path, audio_path, output_path, sync_mode="timeline")


def test_mux_narration_rejects_output_without_audio_stream(tmp_path, monkeypatch) -> None:
    pretend_ffmpeg_available(monkeypatch)
    video_path = tmp_path / "video.mp4"
    audio_path = tmp_path / "audio.wav"
    output_path = tmp_path / "narrated.mp4"
    video_path.write_bytes(b"video")
    audio_path.write_bytes(b"audio")

    def fake_run(command, **kwargs):
        output_path.write_bytes(b"muxed")
        return server.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(server, "probe_media_duration", lambda path: 2.0)
    monkeypatch.setattr(server, "probe_media_streams", lambda path: [{"codec_type": "video"}])
    monkeypatch.setattr(server.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="no audio stream"):
        server.mux_narration_audio(video_path, audio_path, output_path)


def test_create_preview_html_for_video_artifact(tmp_path) -> None:
    job_dir = tmp_path / "job"
    media_dir = job_dir / "media"
    output = media_dir / "videos" / "scene" / "480p15" / "Demo.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"video")
    metadata = {
        "success": True,
        "job_id": "20260505T000000Z-deadbeef",
        "scene_name": "Demo",
        "job_dir": str(job_dir),
        "artifacts": server.discover_artifacts(media_dir),
    }

    preview = server.create_preview_html(metadata)

    assert preview is not None
    assert preview["mime_type"] == "text/html"
    preview_text = (job_dir / "preview.html").read_text(encoding="utf-8")
    assert "<video controls" in preview_text
    assert output.resolve().as_uri() in preview_text


def test_render_scene_tool_result_includes_resource_links(tmp_path) -> None:
    job_dir = tmp_path / "job"
    media_dir = job_dir / "media"
    output = media_dir / "videos" / "scene" / "480p15" / "Demo.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"video")
    metadata = {
        "success": True,
        "job_id": "20260505T000000Z-deadbeef",
        "scene_name": "Demo",
        "job_dir": str(job_dir),
        "artifacts": server.discover_artifacts(media_dir),
    }
    server.create_preview_html(metadata)

    result = server._render_scene_tool_result(metadata)

    assert result.isError is False
    assert result.structuredContent["preview_html"]["mime_type"] == "text/html"
    assert "ui_preview" not in result.structuredContent
    assert not any(item.type == "resource" for item in result.content)
    assert any(item.type == "resource_link" and item.mimeType == "video/mp4" for item in result.content)
    assert not any(
        item.type == "resource_link" and str(item.uri).endswith("/preview.html")
        for item in result.content
    )


def test_render_scene_tool_result_can_inline_tiny_ui_media_when_requested(tmp_path) -> None:
    job_dir = tmp_path / "job"
    media_dir = job_dir / "media"
    output = media_dir / "videos" / "scene" / "480p15" / "Demo.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"video")
    metadata = {
        "success": True,
        "job_id": "20260505T000000Z-deadbeef",
        "scene_name": "Demo",
        "job_dir": str(job_dir),
        "artifacts": server.discover_artifacts(media_dir),
    }
    server.create_preview_html(metadata)

    result = server._render_scene_tool_result(
        metadata,
        include_ui_resource=True,
        max_inline_ui_video_bytes=10,
    )

    assert result.structuredContent["ui_preview"]["inline_media"] is True
    ui_resource = next(
        item for item in result.content
        if item.type == "resource" and item.resource.mimeType == "text/html;profile=mcp-app"
    )
    assert "data:video/mp4;base64," in ui_resource.resource.text


def test_render_scene_tool_result_surfaces_app_video_links(tmp_path, monkeypatch) -> None:
    render_root = tmp_path / "renders"
    monkeypatch.setattr(server, "RENDER_ROOT", render_root)
    job_dir = render_root / "20260505T000000Z-deadbeef"
    media_dir = job_dir / "media"
    output = media_dir / "videos" / "scene" / "480p15" / "Demo.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"video")
    metadata = {
        "success": True,
        "job_id": "20260505T000000Z-deadbeef",
        "scene_name": "Demo",
        "job_dir": str(job_dir),
        "artifacts": server.discover_artifacts(media_dir),
    }
    server.create_preview_html(metadata)

    result = server._render_scene_tool_result(metadata)
    text = result.content[0].text
    access = result.structuredContent["access"]

    assert access["video_stream_url"].startswith("http://127.0.0.1:")
    assert access["preview_stream_url"].startswith("http://127.0.0.1:")
    assert f"Open video: {access['video_stream_url']}" in text
    assert f"Open player: {access['preview_stream_url']}" in text
    assert f"Video path: {output.resolve()}" in text
    assert "HTML preview:" not in text
    assert "Video file URI:" not in text
    assert result.structuredContent["final_response_markdown"] in text
    assert "claude_response_instructions" in result.structuredContent
    assert any(
        item.type == "resource_link" and str(item.uri) == access["video_stream_url"]
        for item in result.content
    )
    assert any(
        item.type == "resource_link" and str(item.uri) == access["preview_stream_url"]
        for item in result.content
    )


def test_get_render_access_returns_compact_latest_links(tmp_path, monkeypatch) -> None:
    render_root = tmp_path / "renders"
    monkeypatch.setattr(server, "RENDER_ROOT", render_root)
    job_dir = render_root / "20260505T000000Z-deadbeef"
    media_dir = job_dir / "media"
    output = media_dir / "videos" / "scene" / "480p15" / "Demo.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"video")
    metadata = {
        "success": True,
        "job_id": "20260505T000000Z-deadbeef",
        "scene_name": "Demo",
        "job_dir": str(job_dir),
        "media_dir": str(media_dir),
        "artifacts": server.discover_artifacts(media_dir),
    }
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    result = server.get_render_access()
    text = result.content[0].text
    access = result.structuredContent["access"]

    assert result.isError is False
    assert result.structuredContent["job_id"] == "20260505T000000Z-deadbeef"
    assert "MANIM RENDER ACCESS" in text
    assert f"Open video: {access['video_stream_url']}" in text
    assert f"Video path: {output.resolve()}" in text
    assert result.structuredContent["final_response_markdown"] in text
    assert set(result.structuredContent) == {
        "success",
        "job_id",
        "scene_name",
        "access",
        "final_response_markdown",
        "claude_response_instructions",
    }


def test_list_renders_reads_metadata(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "RENDER_ROOT", tmp_path / "renders")
    job = tmp_path / "renders" / "20260505T000000Z-deadbeef"
    job.mkdir(parents=True)
    metadata = {"job_id": job.name, "success": True, "artifacts": []}
    (job / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    result = server.list_renders()
    assert result["renders"] == [metadata]


def test_read_render_log_rejects_path_traversal(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "RENDER_ROOT", tmp_path / "renders")
    result = server.read_render_log("../bad")
    assert result["success"] is False
    assert "Invalid job_id" in result["error"]
