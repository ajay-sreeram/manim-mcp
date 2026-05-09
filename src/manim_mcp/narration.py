"""Narration text -> timing plan -> TTS audio -> aligned final track.

Public surface:

* ``split_narration_segments`` -- sentence-level split that respects paragraphs
* ``build_narration_timing_plan`` -- heuristic timing plan (no TTS)
* ``build_measured_narration_timing_plan`` -- timing plan from real TTS durations
* ``synthesize_narration_audio`` -- one TTS call (HF or local Kokoro)
* ``synthesize_segmented_narration_audio`` -- per-sentence TTS + concat + silence
* ``concatenate_audio_segments`` -- ffmpeg concat with inter-segment silence
* ``align_segmented_audio_to_timeline`` -- place audio at *rendered* beat starts
* ``mux_narration_audio`` -- final audio/video mux with bounded retiming
* ``prepare_narration_metadata`` / ``load_prepared_narration`` -- prepared workflow

The single most important quality choice is in
:func:`split_narration_segments` and :func:`concatenate_audio_segments`:
sentences are no longer concatenated back-to-back; each one carries an
explicit ``post_gap_seconds`` that becomes silence in the final audio AND a
matching ``self.wait(...)`` in the visual timeline.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_LOCAL_NARRATION_LANG_CODE,
    DEFAULT_LOCAL_NARRATION_VOICE,
    DEFAULT_NARRATION_MODEL,
    DEFAULT_NARRATION_PROVIDER,
    JOB_ID_RE,
    LOCAL_NARRATION_PROVIDER,
    LOCAL_NARRATION_PROVIDERS,
    MAX_GLOBAL_VIDEO_PTS,
    MAX_VIDEO_EXTEND_SECONDS,
    MIN_GLOBAL_VIDEO_PTS,
    PARAGRAPH_GAP_SECONDS,
    PREPARED_NARRATION_ROOT,
    SEGMENT_AUDIO_OVERFLOW_TOLERANCE,
    SENTENCE_GAP_SECONDS,
    TERMINAL_GAP_SECONDS,
    WORD_RE,
    NarrationAudioMode,
    NarrationSyncMode,
    env_value,
    new_job_id,
    tail,
    tool_env,
)


# ---------------------------------------------------------------------------
# 1. Sentence splitting + per-segment trailing-gap rules
# ---------------------------------------------------------------------------

def _trailing_gap_for(text: str) -> float:
    stripped = text.rstrip()
    if not stripped:
        return SENTENCE_GAP_SECONDS
    last = stripped[-1]
    if last in "?!":
        return TERMINAL_GAP_SECONDS
    if last == ".":
        return SENTENCE_GAP_SECONDS
    if last in ":;":
        return SENTENCE_GAP_SECONDS * 0.7
    if last == ",":
        return SENTENCE_GAP_SECONDS * 0.5
    return SENTENCE_GAP_SECONDS


def split_narration_segments(text: str) -> list[dict[str, Any]]:
    """Split narration into spoken segments, respecting paragraph boundaries.

    Each returned dict has ``text`` and ``post_gap_seconds`` (the silence to
    insert *after* this sentence in the final audio).
    """
    stripped = text.strip()
    if not stripped:
        return []

    paragraphs = [p for p in re.split(r"\n\s*\n", stripped) if p.strip()]
    segments: list[dict[str, Any]] = []

    for para_index, paragraph in enumerate(paragraphs):
        paragraph = re.sub(r"\s+", " ", paragraph).strip()
        if not paragraph:
            continue

        pieces = re.split(r"(?<=[.!?])\s+", paragraph)
        cleaned_pieces: list[str] = [p.strip(" \t-") for p in pieces if p.strip()]

        # Within a single paragraph, merge tiny tail fragments into the
        # previous sentence so a "Right." or "OK." doesn't become its own beat.
        merged: list[str] = []
        for piece in cleaned_pieces:
            words = WORD_RE.findall(piece)
            if len(words) < 3 and merged:
                merged[-1] = f"{merged[-1]} {piece}"
            else:
                merged.append(piece)

        for index, piece in enumerate(merged):
            is_last_in_paragraph = index == len(merged) - 1
            is_last_overall = is_last_in_paragraph and para_index == len(paragraphs) - 1
            if is_last_overall:
                gap = 0.0
            elif is_last_in_paragraph:
                gap = PARAGRAPH_GAP_SECONDS
            else:
                gap = _trailing_gap_for(piece)
            segments.append({"text": piece, "post_gap_seconds": round(gap, 3)})

    return segments


def estimate_spoken_seconds(text: str, *, words_per_minute: float = 165.0) -> float:
    """Rough TTS duration estimate (Kokoro tends to run ~165 wpm)."""
    words = WORD_RE.findall(text)
    word_seconds = len(words) / max(words_per_minute / 60.0, 0.1)
    comma_pause = 0.10 * len(re.findall(r"[,]", text))
    medium_pause = 0.16 * len(re.findall(r"[;:]", text))
    terminal_pause = 0.22 if re.search(r"[.!?]\s*$", text) else 0.06
    number_pause = 0.07 * len(re.findall(r"\b\d+(?:\.\d+)?\b", text))
    return max(0.55, word_seconds + comma_pause + medium_pause + terminal_pause + number_pause)


# ---------------------------------------------------------------------------
# 2. Timing plans
# ---------------------------------------------------------------------------

def _planned_segment(
    index: int,
    text: str,
    duration: float,
    post_gap: float,
    start: float,
    *,
    estimated: float | None = None,
    timing_source: str,
) -> dict[str, Any]:
    end = start + duration
    return {
        "index": index,
        "text": text,
        "word_count": len(WORD_RE.findall(text)),
        "estimated_seconds": round(estimated if estimated is not None else duration, 3),
        "duration_seconds": round(duration, 3),
        "post_gap_seconds": round(post_gap, 3),
        "start_seconds": round(start, 3),
        "end_seconds": round(end, 3),
        "block_seconds": round(duration + post_gap, 3),  # spoken + trailing silence
        "timing_source": timing_source,
    }


def build_narration_timing_plan(
    text: str,
    *,
    total_duration_seconds: float | None = None,
    words_per_minute: float = 165.0,
) -> dict[str, Any]:
    """Build a heuristic timing plan with no TTS measurement."""
    segments = split_narration_segments(text)
    if not segments:
        raise ValueError("narration_text must contain at least one spoken segment.")

    raw_durations = [
        estimate_spoken_seconds(segment["text"], words_per_minute=words_per_minute)
        for segment in segments
    ]
    estimated_total = sum(raw_durations) + sum(s["post_gap_seconds"] for s in segments)
    target_total = (
        total_duration_seconds
        if total_duration_seconds and total_duration_seconds > 0
        else estimated_total
    )
    speech_only_total = max(target_total - sum(s["post_gap_seconds"] for s in segments), 0.5)
    raw_speech_total = sum(raw_durations)
    scale = speech_only_total / raw_speech_total if raw_speech_total > 0 else 1.0

    planned: list[dict[str, Any]] = []
    cursor = 0.0
    for index, (segment, raw_duration) in enumerate(zip(segments, raw_durations, strict=True)):
        duration = max(0.55, raw_duration * scale)
        post_gap = float(segment["post_gap_seconds"])
        planned.append(
            _planned_segment(
                index,
                segment["text"],
                duration,
                post_gap,
                cursor,
                estimated=raw_duration,
                timing_source="heuristic_scaled",
            )
        )
        cursor += duration + post_gap

    return {
        "segment_count": len(planned),
        "word_count": sum(s["word_count"] for s in planned),
        "estimated_total_seconds": round(estimated_total, 3),
        "target_total_seconds": round(cursor, 3),
        "timing_source": "heuristic_scaled",
        "words_per_minute": words_per_minute,
        "segments": planned,
    }


def build_measured_narration_timing_plan(
    segments: list[dict[str, Any]],
    audio_durations: list[float],
    *,
    words_per_minute: float = 165.0,
) -> dict[str, Any]:
    """Build a timing plan from per-segment TTS durations and post-gap silence."""
    if not segments:
        raise ValueError("narration_text must contain at least one spoken segment.")
    if len(segments) != len(audio_durations):
        raise ValueError("segments and audio_durations must have the same length.")

    planned: list[dict[str, Any]] = []
    cursor = 0.0
    estimated_total = 0.0
    for index, (segment, duration) in enumerate(zip(segments, audio_durations, strict=True)):
        text = segment["text"]
        post_gap = float(segment.get("post_gap_seconds", SENTENCE_GAP_SECONDS))
        estimated = estimate_spoken_seconds(text, words_per_minute=words_per_minute)
        estimated_total += estimated + post_gap
        safe_duration = max(0.30, float(duration))
        planned.append(
            _planned_segment(
                index,
                text,
                safe_duration,
                post_gap,
                cursor,
                estimated=estimated,
                timing_source="measured_tts_segments",
            )
        )
        cursor += safe_duration + post_gap

    return {
        "segment_count": len(planned),
        "word_count": sum(s["word_count"] for s in planned),
        "estimated_total_seconds": round(estimated_total, 3),
        "target_total_seconds": round(cursor, 3),
        "timing_source": "measured_tts_segments",
        "words_per_minute": words_per_minute,
        "segments": planned,
    }


# ---------------------------------------------------------------------------
# 3. TTS synthesis
# ---------------------------------------------------------------------------

_LOCAL_KOKORO_PIPELINES: dict[tuple[str, str | None], Any] = {}


def _uses_local_narration_provider(provider: str) -> bool:
    return provider.lower().strip() in LOCAL_NARRATION_PROVIDERS


def narration_tts_backend(provider: str = DEFAULT_NARRATION_PROVIDER) -> str:
    if _uses_local_narration_provider(provider):
        return LOCAL_NARRATION_PROVIDER
    if env_value("HF_TOKEN"):
        return "huggingface-api"
    return LOCAL_NARRATION_PROVIDER


def _require_hf_token() -> str:
    token = env_value("HF_TOKEN")
    if not token:
        raise ValueError("HF_TOKEN is required to generate narration audio with Hugging Face.")
    return token


def _local_kokoro_pipeline(lang_code: str, repo_id: str | None) -> Any:
    cache_key = (lang_code, repo_id)
    if cache_key in _LOCAL_KOKORO_PIPELINES:
        return _LOCAL_KOKORO_PIPELINES[cache_key]

    try:
        from kokoro import KPipeline
    except Exception as exc:
        raise ValueError(
            "Local Kokoro TTS requires the Python package 'kokoro'. Run `uv sync`."
        ) from exc

    try:
        pipeline = KPipeline(lang_code=lang_code, repo_id=repo_id)
    except Exception as exc:
        raise ValueError(
            "Local Kokoro TTS failed to initialize. Ensure 'kokoro', 'soundfile', "
            f"and 'espeakng-loader' are installed. Original error: {exc}"
        ) from exc

    _LOCAL_KOKORO_PIPELINES[cache_key] = pipeline
    return pipeline


def _audio_chunk_to_numpy(audio: Any) -> Any:
    import numpy as np

    if hasattr(audio, "detach") and hasattr(audio, "cpu"):
        audio = audio.detach().cpu().numpy()
    return np.asarray(audio, dtype="float32")


def _ffprobe_duration(path: Path, timeout_seconds: int = 30) -> float | None:
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


def _ffprobe_streams(path: Path, timeout_seconds: int = 30) -> list[dict[str, Any]] | None:
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


def synthesize_local_kokoro_audio(
    text: str,
    output_path: Path,
    *,
    model: str = DEFAULT_NARRATION_MODEL,
    voice: str = DEFAULT_LOCAL_NARRATION_VOICE,
    lang_code: str = DEFAULT_LOCAL_NARRATION_LANG_CODE,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    del timeout_seconds  # Kokoro runs in-process; argument kept for API parity.
    narration = text.strip()
    if not narration:
        raise ValueError("narration_text must not be empty when provided.")

    try:
        import numpy as np
        import soundfile as sf
    except Exception as exc:
        raise ValueError(
            "Local Kokoro TTS requires the Python packages 'kokoro' and 'soundfile'."
        ) from exc

    pipeline = _local_kokoro_pipeline(lang_code, model or None)
    chunks: list[Any] = []
    try:
        for _graphemes, _phonemes, audio in pipeline(narration, voice=voice):
            chunks.append(_audio_chunk_to_numpy(audio))
    except Exception as exc:
        raise ValueError(
            "Local Kokoro TTS failed while generating audio. If this is the first "
            f"local run, model weights may need to download. Original error: {exc}"
        ) from exc

    if not chunks:
        raise ValueError("Local Kokoro TTS returned no audio chunks.")

    audio_data = chunks[0] if len(chunks) == 1 else np.concatenate(chunks)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(output_path, audio_data, 24000)
    if output_path.stat().st_size == 0:
        raise ValueError("Local Kokoro TTS wrote an empty audio file.")

    duration = _ffprobe_duration(output_path)
    streams = _ffprobe_streams(output_path)
    audio_streams = [s for s in streams or [] if s.get("codec_type") == "audio"]
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
        "duration_seconds": duration,
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
    narration = text.strip()
    if not narration:
        raise ValueError("narration_text must not be empty when provided.")

    token = _require_hf_token()
    try:
        from huggingface_hub import InferenceClient
    except Exception as exc:
        raise ValueError("huggingface-hub is required for narration audio.") from exc

    client = InferenceClient(provider=provider, api_key=token, timeout=timeout_seconds)
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

    duration = _ffprobe_duration(output_path)
    streams = _ffprobe_streams(output_path)
    audio_streams = [s for s in streams or [] if s.get("codec_type") == "audio"]
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
        "duration_seconds": duration,
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
    if narration_tts_backend(provider) == LOCAL_NARRATION_PROVIDER:
        return synthesize_local_kokoro_audio(text, output_path, model=model, timeout_seconds=timeout_seconds)
    return synthesize_huggingface_narration_audio(
        text, output_path, model=model, provider=provider,
        timeout_seconds=timeout_seconds, retry_count=retry_count,
    )


# ---------------------------------------------------------------------------
# 4. Audio concat with inter-segment silence
# ---------------------------------------------------------------------------

def _ffmpeg_path() -> str:
    ffmpeg = shutil.which("ffmpeg", path=tool_env().get("PATH"))
    if not ffmpeg:
        raise ValueError("ffmpeg is required to assemble narration audio.")
    return ffmpeg


def concatenate_audio_segments(
    segment_paths: list[Path],
    output_path: Path,
    *,
    inter_segment_silences: list[float] | None = None,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Concatenate per-segment WAVs with explicit silence between them."""
    if not segment_paths:
        raise ValueError("At least one narration segment is required.")

    ffmpeg = _ffmpeg_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    silences = list(inter_segment_silences or [])
    if silences and len(silences) != len(segment_paths):
        raise ValueError("inter_segment_silences must have one entry per segment.")
    if not silences:
        silences = [0.0] * len(segment_paths)

    command: list[str] = [ffmpeg, "-y"]
    input_labels: list[str] = []
    input_index = 0

    for index, segment_path in enumerate(segment_paths):
        command.extend(["-i", str(segment_path)])
        input_labels.append(f"[{input_index}:a:0]")
        input_index += 1

        # Insert silence after every segment except the last one (so the file
        # doesn't end with a long tail).
        gap = float(silences[index] or 0.0)
        if gap > 0.02 and index < len(segment_paths) - 1:
            command.extend([
                "-f", "lavfi",
                "-t", f"{gap:.3f}",
                "-i", "anullsrc=r=24000:cl=mono",
            ])
            input_labels.append(f"[{input_index}:a:0]")
            input_index += 1

    if len(input_labels) == 1:
        command.extend(["-vn", "-ar", "24000", "-ac", "1", str(output_path)])
    else:
        filter_complex = (
            f"{''.join(input_labels)}concat=n={len(input_labels)}:v=0:a=1[a]"
        )
        command.extend([
            "-filter_complex", filter_complex,
            "-map", "[a]", "-ar", "24000", "-ac", "1",
            str(output_path),
        ])

    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout_seconds,
        shell=False, env=tool_env(),
    )
    if completed.returncode != 0:
        raise ValueError(f"ffmpeg narration concat failed: {tail(completed.stderr, 2000)}")

    duration = _ffprobe_duration(output_path)
    streams = _ffprobe_streams(output_path)
    audio_streams = [s for s in streams or [] if s.get("codec_type") == "audio"]
    if streams is not None and not audio_streams:
        raise ValueError("Concatenated narration file does not contain an audio stream.")

    return {
        "path": str(output_path.resolve()),
        "uri": output_path.resolve().as_uri(),
        "size_bytes": output_path.stat().st_size,
        "duration_seconds": duration,
        "audio_stream_count": len(audio_streams) if streams is not None else None,
        "command": command,
        "stdout_tail": tail(completed.stdout),
        "stderr_tail": tail(completed.stderr),
    }


def synthesize_segmented_narration_audio(
    text: str,
    narration_dir: Path,
    *,
    model: str = DEFAULT_NARRATION_MODEL,
    provider: str = DEFAULT_NARRATION_PROVIDER,
    timeout_seconds: int = 120,
    concat_timeout_seconds: int = 300,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Generate one TTS file per sentence, build the timing plan, concat with silences."""
    segments = split_narration_segments(text)
    if not segments:
        raise ValueError("narration_text must contain at least one spoken segment.")

    segments_dir = narration_dir / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)

    segment_infos: list[dict[str, Any]] = []
    audio_durations: list[float] = []
    segment_paths: list[Path] = []
    inter_silences: list[float] = []

    for index, segment in enumerate(segments):
        segment_path = segments_dir / f"{index:03d}.wav"
        audio_info = synthesize_narration_audio(
            segment["text"],
            segment_path,
            model=model,
            provider=provider,
            timeout_seconds=timeout_seconds,
        )
        duration = audio_info.get("duration_seconds")
        if not isinstance(duration, int | float) or duration <= 0:
            raise ValueError(f"Could not measure duration for narration segment {index}.")

        segment_infos.append({
            "index": index,
            "text": segment["text"],
            "post_gap_seconds": segment["post_gap_seconds"],
            "path": audio_info["path"],
            "uri": audio_info["uri"],
            "size_bytes": audio_info["size_bytes"],
            "duration_seconds": round(float(duration), 3),
            "audio_stream_count": audio_info.get("audio_stream_count"),
        })
        audio_durations.append(float(duration))
        segment_paths.append(Path(audio_info["path"]))
        inter_silences.append(float(segment["post_gap_seconds"]))

    output_path = narration_dir / "narration.wav"
    concat_info = concatenate_audio_segments(
        segment_paths, output_path,
        inter_segment_silences=inter_silences,
        timeout_seconds=concat_timeout_seconds,
    )

    timing_plan = build_measured_narration_timing_plan(segments, audio_durations)
    measured_total = sum(audio_durations) + sum(inter_silences[:-1])  # last silence is dropped
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
        "inter_segment_silences": [round(s, 3) for s in inter_silences],
        "measured_segments_total_seconds": round(measured_total, 3),
        "concat_duration_delta_seconds": round(duration_delta, 3)
        if duration_delta is not None
        else None,
        "concat_command": concat_info["command"],
        "concat_stdout_tail": concat_info["stdout_tail"],
        "concat_stderr_tail": concat_info["stderr_tail"],
    }
    return audio, timing_plan


# ---------------------------------------------------------------------------
# 5. Timeline-aligned audio: place each spoken segment at its rendered start
# ---------------------------------------------------------------------------

def align_segmented_audio_to_timeline(
    audio: dict[str, Any],
    timeline_actual: dict[str, Any],
    output_path: Path,
    *,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    segment_infos = audio.get("segments") or []
    timeline_events = timeline_actual.get("timeline_events") or []
    if not segment_infos or not timeline_events:
        return audio

    ffmpeg = _ffmpeg_path()

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
    command: list[str] = [ffmpeg, "-y"]
    input_labels: list[str] = []
    silence_gaps: list[dict[str, Any]] = []
    overflow_warnings: list[dict[str, Any]] = []
    cursor = 0.0
    input_index = 0

    sorted_segments = sorted(segment_infos, key=lambda item: int(item.get("index", 0)))
    for segment in sorted_segments:
        index = int(segment.get("index", 0))
        segment_path = Path(segment["path"])
        segment_duration = float(segment.get("duration_seconds") or 0.0)
        post_gap = float(segment.get("post_gap_seconds", SENTENCE_GAP_SECONDS))
        start = max(0.0, float(starts.get(index, cursor)))
        gap = max(0.0, start - cursor)

        # If the rendered visual beat starts before the previous audio finished
        # (audio overran its visual), record an overflow warning.
        if start < cursor - SEGMENT_AUDIO_OVERFLOW_TOLERANCE:
            overflow_warnings.append({
                "segment_index": index,
                "audio_overrun_seconds": round(cursor - start, 3),
                "audio_duration_seconds": round(segment_duration, 3),
            })

        if gap > 0.02:
            command.extend([
                "-f", "lavfi", "-t", f"{gap:.3f}",
                "-i", "anullsrc=r=24000:cl=mono",
            ])
            input_labels.append(f"[{input_index}:a:0]")
            silence_gaps.append({
                "before_segment_index": index,
                "duration_seconds": round(gap, 3),
                "start_seconds": round(cursor, 3),
                "end_seconds": round(start, 3),
            })
            input_index += 1
            cursor = start

        command.extend(["-i", str(segment_path)])
        input_labels.append(f"[{input_index}:a:0]")
        input_index += 1
        cursor = max(cursor, start) + max(segment_duration, 0.0)

        # Always insert the planned post-gap silence (except after the last
        # segment), independent of the rendered timeline -- this ensures
        # natural pauses between sentences even when beats were back-to-back.
        if post_gap > 0.02 and segment is not sorted_segments[-1]:
            command.extend([
                "-f", "lavfi", "-t", f"{post_gap:.3f}",
                "-i", "anullsrc=r=24000:cl=mono",
            ])
            input_labels.append(f"[{input_index}:a:0]")
            input_index += 1
            cursor += post_gap

    if not input_labels:
        return audio

    command.extend([
        "-filter_complex",
        f"{''.join(input_labels)}concat=n={len(input_labels)}:v=0:a=1[a]",
        "-map", "[a]", "-ar", "24000", "-ac", "1",
        str(output_path),
    ])

    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout_seconds,
        shell=False, env=tool_env(),
    )
    if completed.returncode != 0:
        raise ValueError(f"ffmpeg timeline audio alignment failed: {tail(completed.stderr, 2000)}")

    output_duration = _ffprobe_duration(output_path)
    output_streams = _ffprobe_streams(output_path)
    output_audio_streams = [s for s in output_streams or [] if s.get("codec_type") == "audio"]
    if output_streams is not None and not output_audio_streams:
        raise ValueError("Timeline-aligned narration file does not contain an audio stream.")

    aligned = dict(audio)
    aligned.update({
        "path": str(output_path.resolve()),
        "uri": output_path.resolve().as_uri(),
        "source_path": audio.get("path"),
        "source_uri": audio.get("uri"),
        "timeline_aligned": True,
        "timeline_alignment_source": "manim_scene_time",
        "timeline_silence_gaps": silence_gaps,
        "timeline_silence_gap_count": len(silence_gaps),
        "timeline_silence_total_seconds": round(
            sum(gap["duration_seconds"] for gap in silence_gaps), 3
        ),
        "audio_overflow_warnings": overflow_warnings,
        "size_bytes": output_path.stat().st_size,
        "duration_seconds": output_duration or round(cursor, 3),
        "audio_stream_count": len(output_audio_streams)
        if output_streams is not None
        else None,
        "alignment_command": command,
        "alignment_stdout_tail": tail(completed.stdout),
        "alignment_stderr_tail": tail(completed.stderr),
    })
    return aligned


# ---------------------------------------------------------------------------
# 6. Mux audio into the rendered video, with bounded retiming
# ---------------------------------------------------------------------------

def mux_narration_audio(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    *,
    sync_mode: NarrationSyncMode = "fit",
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    ffmpeg = shutil.which("ffmpeg", path=tool_env().get("PATH"))
    if not ffmpeg:
        raise ValueError("ffmpeg is required to mux narration audio into the video.")

    video_duration = _ffprobe_duration(video_path)
    audio_duration = _ffprobe_duration(audio_path)
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
            "Could not measure " + " and ".join(missing) + " duration with ffprobe. "
            "Narration sync requires ffprobe."
        )

    capped_extra = False
    if extra_duration > MAX_VIDEO_EXTEND_SECONDS:
        capped_extra = True
        # We will still render, but with extension capped; quality.py reports the issue.
        extra_duration = MAX_VIDEO_EXTEND_SECONDS

    fit_clamped = False
    if (
        sync_mode == "fit"
        and video_duration is not None
        and audio_duration is not None
        and video_duration > 0
        and abs(audio_duration - video_duration) > 0.1
    ):
        raw_factor = max(audio_duration / video_duration, 0.05)
        clamped_factor = min(max(raw_factor, MIN_GLOBAL_VIDEO_PTS), MAX_GLOBAL_VIDEO_PTS)
        if clamped_factor != raw_factor:
            fit_clamped = True
        # If the clamp is so tight we can't cover the audio, fall back to
        # extending/padding rather than producing badly-paced video.
        if fit_clamped and raw_factor > MAX_GLOBAL_VIDEO_PTS:
            mode = "extend_video_after_fit_clamp"
            command = [
                ffmpeg, "-y", "-i", str(video_path), "-i", str(audio_path),
                "-filter_complex",
                (
                    f"[0:v]setpts={MAX_GLOBAL_VIDEO_PTS:.6f}*PTS,"
                    f"tpad=stop_mode=clone:stop_duration={extra_duration:.3f}[v]"
                ),
                "-map", "[v]", "-map", "1:a:0",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest",
                "-movflags", "+faststart",
                str(output_path),
            ]
        elif fit_clamped and raw_factor < MIN_GLOBAL_VIDEO_PTS:
            mode = "pad_audio_after_fit_clamp"
            command = [
                ffmpeg, "-y", "-i", str(video_path), "-i", str(audio_path),
                "-filter_complex",
                (
                    f"[0:v]setpts={MIN_GLOBAL_VIDEO_PTS:.6f}*PTS[v];"
                    f"[1:a:0]apad=whole_dur={video_duration * MIN_GLOBAL_VIDEO_PTS:.3f}[a]"
                ),
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest",
                "-movflags", "+faststart",
                str(output_path),
            ]
        else:
            mode = "fit_video_to_audio"
            command = [
                ffmpeg, "-y", "-i", str(video_path), "-i", str(audio_path),
                "-filter_complex", f"[0:v]setpts={clamped_factor:.8f}*PTS[v]",
                "-map", "[v]", "-map", "1:a:0",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest",
                "-movflags", "+faststart",
                str(output_path),
            ]
    elif extra_duration > 0.1:
        mode = "extend_video"
        command = [
            ffmpeg, "-y", "-i", str(video_path), "-i", str(audio_path),
            "-filter_complex",
            f"[0:v]tpad=stop_mode=clone:stop_duration={extra_duration:.3f},setpts=PTS-STARTPTS[v]",
            "-map", "[v]", "-map", "1:a:0",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            "-movflags", "+faststart",
            str(output_path),
        ]
    elif (
        video_duration is not None
        and audio_duration is not None
        and video_duration - audio_duration > 0.1
    ):
        mode = "pad_audio"
        command = [
            ffmpeg, "-y", "-i", str(video_path), "-i", str(audio_path),
            "-filter_complex", f"[1:a:0]apad=whole_dur={video_duration:.3f}[a]",
            "-map", "0:v:0", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac",
            "-shortest", "-movflags", "+faststart",
            str(output_path),
        ]
    else:
        mode = "mux_audio"
        command = [
            ffmpeg, "-y", "-i", str(video_path), "-i", str(audio_path),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac",
            "-movflags", "+faststart",
            str(output_path),
        ]

    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout_seconds,
        shell=False, env=tool_env(),
    )
    if completed.returncode != 0:
        raise ValueError(f"ffmpeg narration mux failed: {tail(completed.stderr, 2000)}")

    output_duration = _ffprobe_duration(output_path)
    output_streams = _ffprobe_streams(output_path)
    output_audio_streams = [s for s in output_streams or [] if s.get("codec_type") == "audio"]
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
        "fit_clamped": fit_clamped,
        "extend_capped": capped_extra,
        "video_duration_seconds": video_duration,
        "audio_duration_seconds": audio_duration,
        "duration_delta_seconds": duration_delta,
        "output_duration_seconds": output_duration,
        "output_audio_stream_count": len(output_audio_streams)
        if output_streams is not None
        else None,
        "command": command,
        "stdout_tail": tail(completed.stdout),
        "stderr_tail": tail(completed.stderr),
    }


# ---------------------------------------------------------------------------
# 7. Public-friendly metadata (drops absolute paths and command logs)
# ---------------------------------------------------------------------------

def _relative_to_any_root(path: Path, roots: list[Path]) -> str:
    resolved = path.resolve()
    for root in roots:
        try:
            return resolved.relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
    return path.name


def public_audio_metadata(audio: dict[str, Any], roots: list[Path]) -> dict[str, Any]:
    omitted_keys = {
        "uri", "source_uri", "source_path",
        "concat_command", "concat_stdout_tail", "concat_stderr_tail",
        "alignment_command", "alignment_stdout_tail", "alignment_stderr_tail",
    }

    def convert(value: Any, key: str | None = None) -> Any:
        if key in omitted_keys:
            return None
        if key == "path" and isinstance(value, str):
            return _relative_to_any_root(Path(value), roots)
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for k, v in value.items():
                converted = convert(v, k)
                if converted is not None:
                    cleaned[k] = converted
            return cleaned
        if isinstance(value, list):
            return [c for item in value if (c := convert(item)) is not None]
        return value

    return convert(audio)


def hydrate_prepared_audio_metadata(audio: dict[str, Any], root: Path) -> dict[str, Any]:
    def hydrate(value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            return {k: hydrate(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [hydrate(item) for item in value]
        if key == "path" and isinstance(value, str):
            path = Path(value)
            resolved = path if path.is_absolute() else root / path
            return str(resolved.resolve())
        return value

    hydrated = hydrate(audio)

    def add_uris(value: Any) -> None:
        if isinstance(value, dict):
            path = value.get("path")
            if isinstance(path, str):
                try:
                    value["uri"] = Path(path).resolve().as_uri()
                except Exception:
                    pass
            for v in value.values():
                add_uris(v)
        elif isinstance(value, list):
            for item in value:
                add_uris(item)

    add_uris(hydrated)
    return hydrated


# ---------------------------------------------------------------------------
# 8. prepare_narration / load_prepared_narration
# ---------------------------------------------------------------------------

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
    text = narration_text.strip()
    if not text:
        raise ValueError("narration_text must not be empty.")
    if narration_audio_mode not in {"segmented", "single"}:
        raise ValueError("narration_audio_mode must be one of: segmented, single.")

    prepared_id = new_job_id()
    narration_dir = PREPARED_NARRATION_ROOT / prepared_id
    audio_dir = narration_dir / "narration"
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
                text, audio_dir,
                model=narration_model, provider=narration_provider,
                timeout_seconds=narration_tts_timeout_seconds,
                concat_timeout_seconds=narration_mux_timeout_seconds,
            )
        else:
            output_path = audio_dir / "narration.wav"
            audio = synthesize_narration_audio(
                text, output_path,
                model=narration_model, provider=narration_provider,
                timeout_seconds=narration_tts_timeout_seconds,
            )
            timing_plan = build_narration_timing_plan(
                text, total_duration_seconds=audio.get("duration_seconds"),
            )

        timing_path.write_text(json.dumps(timing_plan, indent=2) + "\n", encoding="utf-8")
        public_audio = public_audio_metadata(audio, [narration_dir])
        metadata.update({
            "success": True,
            "duration_seconds": round(time.monotonic() - started, 3),
            "audio": public_audio,
            "timing_plan": timing_plan,
            "usage": (
                "Read every segment in timing_plan['segments'] before writing scene code. "
                "Each segment.text is a sentence that will be spoken aloud; each "
                "segment.duration_seconds is its measured TTS length; segment.post_gap_seconds "
                "is the silence we insert after it. In your construct(), call "
                "tl = narration_timeline(self) and bind segment i with tl.play_segment(i, ...)"
                " or tl.wait_segment(i). The injected helper handles the post-gap automatically."
            ),
        })
    except Exception as exc:
        metadata.update({
            "success": False,
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": f"{type(exc).__name__}: {exc}",
        })

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
    metadata["audio"] = hydrate_prepared_audio_metadata(metadata["audio"], narration_dir)
    return metadata
