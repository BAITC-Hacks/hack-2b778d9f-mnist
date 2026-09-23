import json
import gc
import math
import os
import subprocess
import wave
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


def transcribe_turns(wav: Path) -> list[dict]:
    snapshot = _local_model("ASR_MODEL_PATH")
    import torch
    from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline

    dtype = torch.float16 if torch.cuda.is_available() else torch.float32
    model = AutoModelForSpeechSeq2Seq.from_pretrained(
        str(snapshot), local_files_only=True, trust_remote_code=False, dtype=dtype,
    )
    # The fine-tuned snapshot forces "kk". Detect language for Russian and
    # mixed meetings instead of inheriting that training-time default.
    model.generation_config.language = None
    processor = AutoProcessor.from_pretrained(
        str(snapshot), local_files_only=True, trust_remote_code=False,
    )
    recognizer = pipeline(
        "automatic-speech-recognition",
        model=model,
        tokenizer=processor.tokenizer,
        feature_extractor=processor.feature_extractor,
        device="cuda" if torch.cuda.is_available() else "cpu",
        dtype=dtype,
        chunk_length_s=30,
        batch_size=1,
    )
    samples = _samples(wav)
    duration = len(samples) / 16000
    try:
        result = recognizer({"raw": samples, "sampling_rate": 16000}, return_timestamps=True,
                            generate_kwargs={"task": "transcribe"})
    finally:
        del recognizer, model, processor
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    segments = result.get("chunks") or []
    if not segments and result.get("text", "").strip():
        segments = [{"text": result["text"], "timestamp": (None, None)}]
    turns = []
    for index, segment in enumerate(segments):
        timestamp = segment.get("timestamp") or (None, None)
        start = timestamp[0] if len(timestamp) > 0 else None
        end = timestamp[1] if len(timestamp) > 1 else None
        uncertain = start is None or end is None
        if start is None:
            start = turns[-1]["end"] if turns else 0.0
        if end is None:
            following = segments[index + 1].get("timestamp") if index + 1 < len(segments) else None
            end = following[0] if following and following[0] is not None else duration
        start = max(0.0, min(float(start), duration))
        end = max(start, min(float(end), duration))
        turns.append({
            "id": f"t{index + 1}",
            "start": start,
            "end": end,
            "text": segment.get("text", "").strip(),
            "timestamp_uncertain": uncertain,
        })
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
