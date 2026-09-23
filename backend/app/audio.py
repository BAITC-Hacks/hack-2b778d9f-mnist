import json
import gc
import math
import os
import subprocess
import wave
import tempfile

from app.model_artifacts import _validate_ct2_artifact, _validate_diarization_artifact, check_model_dependencies
from pathlib import Path


MAX_AUDIO_SECONDS = 600


def prepare_audio(source: Path, target: Path, *, allowed_formats: set[str] | None = None) -> float:
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
            "-show_entries", "format=duration,format_name",
            "-show_entries", "stream=codec_type", "-of", "json", str(source),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if probe.returncode:
        raise ValueError("Unable to probe audio")
    try:
        metadata = json.loads(probe.stdout)
        duration = float(metadata["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("Invalid audio metadata") from None
    formats = set(metadata.get("format", {}).get("format_name", "").split(","))
    if allowed_formats is not None and not formats & allowed_formats:
        raise ValueError("Upload a WAV or MP3 recording")
    if "audio" not in {stream.get("codec_type") for stream in metadata.get("streams", [])}:
        raise ValueError("Input contains no audio stream")
    if not math.isfinite(duration) or duration <= 0 or duration > MAX_AUDIO_SECONDS:
        raise ValueError("Audio duration must be between 0 and 600 seconds")

    converted = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-xerror", "-y",
            "-protocol_whitelist", "file,pipe", "-i", str(source),
            "-t", str(MAX_AUDIO_SECONDS + 1), "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", "-f", "wav", str(target),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if converted.returncode:
        Path(target).unlink(missing_ok=True)
        raise ValueError("Unable to convert audio")
    with wave.open(str(target), "rb") as decoded:
        actual_duration = decoded.getnframes() / decoded.getframerate()
    if not 0 < actual_duration <= MAX_AUDIO_SECONDS:
        Path(target).unlink(missing_ok=True)
        raise ValueError("Decoded audio duration must be between 0 and 600 seconds")
    return actual_duration


def _local_model(variable: str) -> Path:
    legacy = {"ASR_MODEL_ARTIFACT": "ASR_MODEL_PATH",
              "DIARIZATION_MODEL_ARTIFACT": "PYANNOTE_DIARIZATION_MODEL"}
    value = os.environ.get(variable) or os.environ.get(legacy.get(variable, ""))
    if not value or not Path(value).is_dir():
        raise RuntimeError(f"Set {variable} to a provisioned local model directory")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["PYANNOTE_METRICS_ENABLED"] = "0"
    return Path(value)


def _samples(wav: Path):
    import numpy as np
    with wave.open(str(wav), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getframerate() != 16000:
            raise ValueError("Model input must be mono 16-bit PCM WAV at 16 kHz")
        return np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32) / 32768.0


def _speech_windows(duration: float, speaker_intervals=None, *, max_seconds: float = 25.0) -> list[tuple[float, float]]:
    """Partition speech on the sample clock and combine tiny adjacent fragments."""
    if not math.isfinite(duration) or duration <= 0 or not 0 < max_seconds <= 25:
        raise ValueError("Invalid ASR window duration")
    total = round(duration * 16000)
    maximum = max(1, round(max_seconds * 16000))
    minimum = 1600  # A fragment under 100 ms cannot support independent decoding.
    merge_gap = 4800
    if speaker_intervals is None:
        regions = [(0, total, frozenset())]
    else:
        intervals = []
        for start, end, speaker in speaker_intervals:
            if not (math.isfinite(start) and math.isfinite(end) and start < end):
                raise ValueError("Invalid diarization interval")
            start, end = max(0, round(start * 16000)), min(total, round(end * 16000))
            if end > start:
                intervals.append((start, end, speaker))
        boundaries = sorted({point for left, right, _ in intervals for point in (left, right)})
        regions = []
        for start, end in zip(boundaries, boundaries[1:]):
            speakers = frozenset(speaker for left, right, speaker in intervals if left < end and right > start)
            if not speakers:
                continue
            if regions and regions[-1][2] == speakers and start - regions[-1][1] <= merge_gap:
                regions[-1] = (regions[-1][0], end, speakers)
            else:
                regions.append((start, end, speakers))
    windows = []
    for start, end, _ in regions:
        while start < end:
            right = min(start + maximum, end)
            if maximum >= minimum and 0 < end - right < minimum:
                right = (start + end) // 2
            windows.append((start, right))
            start = right
    index = 0
    while index < len(windows):
        start, end = windows[index]
        if end - start < minimum:
            if (index > 0 and start - windows[index - 1][1] <= merge_gap
                    and end - windows[index - 1][0] <= maximum):
                windows[index - 1] = (windows[index - 1][0], end)
                windows.pop(index)
                index -= 1
                continue
            if (index + 1 < len(windows) and windows[index + 1][0] - end <= merge_gap
                    and windows[index + 1][1] - start <= maximum):
                windows[index] = (start, windows[index + 1][1])
                windows.pop(index + 1)
                continue
        index += 1
    return [(start / 16000, end / 16000) for start, end in windows]


def transcribe_turns(wav: Path, speaker_intervals=None) -> list[dict]:
    """Decode bounded speech windows with the local Whisper Turbo CT2 artifact."""
    snapshot = _local_model("ASR_MODEL_ARTIFACT")
    samples = _samples(wav)
    windows = _speech_windows(len(samples) / 16000, speaker_intervals)
    if not windows or all(round(end * 16000) - round(start * 16000) < 1600
                          for start, end in windows):
        return []
    _validate_ct2_artifact(snapshot)
    check_model_dependencies("asr")
    from faster_whisper import WhisperModel

    model = WhisperModel(
        str(snapshot), device=os.environ.get("ASR_DEVICE", "cpu"),
        compute_type=os.environ.get("ASR_COMPUTE_TYPE", "int8"), local_files_only=True,
    )
    turns = []
    try:
        for start, end in windows:
            segments, _ = model.transcribe(
                samples[round(start * 16000):round(end * 16000)],
                language=None, multilingual=True, word_timestamps=True,
                vad_filter=True, condition_on_previous_text=False,
            )
            for segment in segments:
                text = segment.text.strip()
                if not text:
                    continue
                left, right = float(segment.start), float(segment.end)
                if not (math.isfinite(left) and math.isfinite(right) and 0 <= left < right):
                    raise ValueError("ASR returned invalid segment timestamps")
                left, right = start + left, min(start + right, end)
                if left >= right:
                    raise ValueError("ASR returned timestamps outside its speech window")
                turns.append({"id": f"t{len(turns) + 1}", "start": left, "end": right,
                              "text": text, "timestamp_uncertain": False})
    finally:
        del model
        gc.collect()
    return turns


def diarize_turns(wav: Path) -> list[tuple[float, float, str]]:
    snapshot = _local_model("DIARIZATION_MODEL_ARTIFACT")
    document = _validate_diarization_artifact(snapshot)
    check_model_dependencies("diarization")
    import torch
    import yaml
    from pyannote.audio import Pipeline

    with tempfile.TemporaryDirectory(prefix="meeting-pipeline-") as temporary:
        config = Path(temporary) / "config.yaml"
        config.write_text(yaml.safe_dump(document), encoding="utf-8")
        diarizer = Pipeline.from_pretrained(str(config))
        try:
            diarizer.to(torch.device(os.environ.get("DIARIZATION_DEVICE", "cpu")))
            output = diarizer({"waveform": torch.from_numpy(_samples(wav)).unsqueeze(0), "sample_rate": 16000})
        finally:
            del diarizer
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
    annotation = getattr(output, "speaker_diarization", output)
    return [
        (float(turn.start), float(turn.end), str(speaker))
        for turn, _, speaker in annotation.itertracks(yield_label=True)
    ]
