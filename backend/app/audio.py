import json
import gc
import math
import os
import subprocess
import wave

from pathlib import Path


MAX_AUDIO_SECONDS = 600
MIN_ASR_SAMPLES = 8000  # Decode at least 0.5 s with neighboring context when available.


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
    value = os.environ.get(variable)
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
    minimum = MIN_ASR_SAMPLES
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
    """Decode short speech windows; their source boundaries supply timestamps."""
    snapshot = _local_model("ASR_MODEL_PATH")
    samples = _samples(wav)
    windows = _speech_windows(len(samples) / 16000, speaker_intervals)
    if not windows or all(round(end * 16000) - round(start * 16000) < MIN_ASR_SAMPLES
                          for start, end in windows):
        return []
    import torch
    from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    model = AutoModelForSpeechSeq2Seq.from_pretrained(
        str(snapshot), local_files_only=True, trust_remote_code=False, dtype=dtype,
    ).to(device)
    model.generation_config.language = None
    processor = AutoProcessor.from_pretrained(
        str(snapshot), local_files_only=True, trust_remote_code=False,
    )
    turns = []
    try:
        for start, end in windows:
            segment = samples[round(start * 16000):round(end * 16000)]
            if len(segment) < MIN_ASR_SAMPLES:
                turns.append({"id": f"t{len(turns) + 1}", "start": start, "end": end,
                              "text": "[Короткий неразборчивый фрагмент]", "timestamp_uncertain": True})
                continue
            inputs = processor(
                segment, sampling_rate=16000,
                return_tensors="pt", return_attention_mask=True,
            ).to(device)
            inputs["input_features"] = inputs["input_features"].to(dtype)
            with torch.inference_mode():
                tokens = model.generate(
                    **inputs, task="transcribe", return_timestamps=False,
                    do_sample=False, max_new_tokens=440,
                )
            if len(tokens[0]) >= 440:
                raise ValueError("ASR exceeded its output limit; review the recording")
            text = processor.batch_decode(tokens, skip_special_tokens=True)[0].strip()
            if text:
                turns.append({"id": f"t{len(turns) + 1}", "start": start, "end": end,
                              "text": text, "timestamp_uncertain": False})
    finally:
        del model, processor
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    return turns


def diarize_turns(wav: Path) -> list[tuple[float, float, str]]:
    snapshot = _local_model("PYANNOTE_DIARIZATION_MODEL")
    import torch
    from pyannote.audio import Pipeline

    diarizer = Pipeline.from_pretrained(str(snapshot))
    diarizer.to(torch.device("cuda" if torch.cuda.is_available() else "cpu"))
    try:
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
