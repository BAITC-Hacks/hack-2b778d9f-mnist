import gc
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from .config import Settings
from .models import SpeakerTurn, TranscriptSegment


class SetupError(RuntimeError):
    """Safe, actionable message that may be returned by the API."""


def sanitize_filename(name: str) -> str:
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^\w. -]", "_", name, flags=re.UNICODE).strip(" .")[:160]
    if not name or name.split(".")[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *[f"COM{i}" for i in range(10)],
        *[f"LPT{i}" for i in range(10)],
    }:
        name = "audio_" + name
    return name


def normalize_audio(source: Path, destination: Path, executable: str) -> None:
    binary = shutil.which(executable)
    if not binary:
        raise SetupError(
            "ffmpeg is unavailable. Install ffmpeg and set FFMPEG_PATH to its executable."
        )
    result = subprocess.run(
        [
            binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ],
        capture_output=True,
        timeout=1800,
        check=False,
    )
    if result.returncode:
        raise SetupError("ffmpeg could not decode this upload. Check that it contains valid audio.")


class ASR(Protocol):
    def transcribe(self, path: Path) -> list[TranscriptSegment]: ...


class Diarizer(Protocol):
    def diarize(self, path: Path) -> list[SpeakerTurn]: ...


def offline() -> None:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["PYANNOTE_METRICS_ENABLED"] = "0"


def release_gpu() -> None:
    gc.collect()
    import sys

    if "torch" in sys.modules:
        torch = sys.modules["torch"]
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


class FasterWhisperASR:
    def __init__(self, settings: Settings):
        self.settings = settings

    def transcribe(self, path: Path) -> list[TranscriptSegment]:
        offline()
        try:
            from faster_whisper import WhisperModel

            model = WhisperModel(
                self.settings.asr_model,
                device=self.settings.asr_device,
                compute_type=self.settings.asr_compute_type,
                local_files_only=True,
            )
        except (ImportError, OSError, ValueError):
            raise SetupError(
                "Install .[models] and pre-download a faster-whisper model; set ASR_MODEL to its local directory."
            ) from None
        try:
            segments, _ = model.transcribe(
                str(path), language=None, multilingual=True, word_timestamps=True, vad_filter=True
            )
            result: list[TranscriptSegment] = []
            for segment in segments:
                # Preserve word times for speaker boundaries; group after diarization.
                if segment.words:
                    result.extend(
                        TranscriptSegment(start=w.start, end=w.end, text=w.word)
                        for w in segment.words
                        if w.word.strip()
                    )
                else:
                    result.append(
                        TranscriptSegment(start=segment.start, end=segment.end, text=segment.text)
                    )
            return result
        finally:
            del model
            release_gpu()


class PyannoteDiarizer:
    def __init__(self, settings: Settings):
        self.settings = settings

    def diarize(self, path: Path) -> list[SpeakerTurn]:
        offline()
        if not self.settings.diarization_model:
            raise SetupError(
                "Set DIARIZATION_MODEL to a locally downloaded pyannote community-1 directory."
            )
        try:
            import soundfile as sf
            import torch
            from pyannote.audio import Pipeline

            pipeline = Pipeline.from_pretrained(self.settings.diarization_model)
            pipeline.to(torch.device(self.settings.diarization_device))
        except (ImportError, OSError, ValueError):
            raise SetupError(
                "Install .[models] and download all diarization weights before offline processing."
            ) from None
        try:
            waveform, rate = sf.read(path, dtype="float32", always_2d=True)
            output = pipeline(
                {"waveform": torch.from_numpy(waveform.T.copy()), "sample_rate": rate}
            )
            annotation = getattr(output, "exclusive_speaker_diarization", None)
            if annotation is None:
                annotation = getattr(output, "speaker_diarization", output)
            return [
                SpeakerTurn(speaker=label, start=turn.start, end=turn.end)
                for turn, _, label in annotation.itertracks(yield_label=True)
            ]
        finally:
            del pipeline
            release_gpu()


def merge_speakers(
    segments: list[TranscriptSegment], turns: list[SpeakerTurn]
) -> list[TranscriptSegment]:
    result: list[TranscriptSegment] = []
    labels = {
        label: f"SPEAKER_{i:02d}" for i, label in enumerate(dict.fromkeys(t.speaker for t in turns))
    }
    for segment in segments:
        overlaps = [
            (max(0, min(segment.end, t.end) - max(segment.start, t.start)), t) for t in turns
        ]
        best = max(overlaps, key=lambda pair: pair[0], default=None)
        speaker = labels[best[1].speaker] if best and best[0] > 0 else "UNKNOWN"
        if (
            result
            and result[-1].speaker == speaker
            and segment.start - result[-1].end < 1.5
            and segment.end - result[-1].start <= 20
            and len(result[-1].text) < 700
        ):
            result[-1].text += " " + segment.text.strip()
            result[-1].end = segment.end
        else:
            result.append(
                segment.model_copy(update={"speaker": speaker, "text": segment.text.strip()})
            )
    return result
