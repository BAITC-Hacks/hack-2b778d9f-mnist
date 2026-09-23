import gc
import importlib
import os
import re
import shutil
import subprocess
import tempfile
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol, cast

from .config import Settings
from .models import SpeakerTurn, TranscriptSegment


class SetupError(RuntimeError):
    """Safe, actionable message that may be returned by the API."""


def _profile_value(profile: Any, name: str, legacy: str | None = None) -> object:
    """Read profile settings while retaining support for the original Settings API."""
    if hasattr(profile, name):
        return getattr(profile, name)
    if legacy is not None and hasattr(profile, legacy):
        return getattr(profile, legacy)
    if name == "device":
        return "cpu"
    if name == "compute_type":
        return "int8"
    raise SetupError("The selected local model profile is incomplete. Select a valid profile.")


def _local_artifact(profile: object, kind: str) -> Path:
    value = _profile_value(profile, "runtime_artifact")
    if not isinstance(value, str) or not value.strip():
        raise SetupError(f"The {kind} profile has no local model artifact. Configure its local artifact path.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SetupError(f"The {kind} model artifact is unavailable locally. Configure an absolute local model directory.")
    if not path.is_dir():
        raise SetupError(f"The {kind} model artifact is unavailable locally. Configure a complete local model directory.")
    return path


def _validate_ct2_artifact(path: Path) -> None:
    required = ("config.json", "model.bin")
    if not path.is_dir() or not all((path / name).is_file() for name in required) or not any(
        (path / name).is_file() for name in ("tokenizer.json", "vocabulary.json")
    ) or not (path / "preprocessor_config.json").is_file():
        raise SetupError("The ASR artifact is not a complete local CTranslate2 model. Convert and bundle the model locally.")


def _validate_diarization_artifact(path: Path) -> dict[str, Any]:
    """Validate pipeline references and return a loader-ready config with absolute weight paths."""
    config = path / "config.yaml"
    if not config.is_file():
        raise SetupError("The local diarization artifact must include config.yaml and all dependent weights.")
    try:
        text = config.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise SetupError("The local diarization config is invalid. Provide a complete local pipeline artifact.") from None
    try:
        yaml = importlib.import_module("yaml")
    except ImportError:
        raise SetupError("Install .[models] to validate the local diarization config before processing.") from None
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        raise SetupError("The local diarization config is invalid. Provide a complete local pipeline artifact.") from None
    message = "The diarization artifact references missing or remote dependencies. Bundle local weight files."
    if not isinstance(document, dict) or not isinstance(document.get("pipeline"), dict):
        raise SetupError(message)
    params = document["pipeline"].get("params")
    if not isinstance(params, dict):
        raise SetupError(message)

    def local_file(value: object) -> str:
        if not isinstance(value, str) or not value.strip() or any(c in value for c in "\x00\r\n"):
            raise SetupError(message)
        if "://" in value or "@" in value or re.match(r"^[A-Za-z]:(?![/\\])", value):
            raise SetupError(message)
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = path / candidate
        candidate = candidate.resolve()
        if not candidate.is_file():
            raise SetupError(message)
        return str(candidate)

    def reference(value: object) -> object:
        if isinstance(value, str):
            return local_file(value)
        if isinstance(value, dict) and value and set(value) <= {"checkpoint_path", "pretrained", "weights"}:
            return {key: local_file(child) for key, child in value.items()}
        raise SetupError(message)

    for key in ("segmentation", "embedding"):
        if key not in params:
            raise SetupError(message)
        params[key] = reference(params[key])

    def validate_extra_references(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"segmentation", "embedding"}:
                    value[key] = reference(child)
                elif key in {"checkpoint_path", "pretrained", "weights", "model", "model_path"}:
                    value[key] = local_file(child)
                else:
                    validate_extra_references(child)
        elif isinstance(value, list):
            for child in value:
                validate_extra_references(child)

    validate_extra_references(document)
    return document


def check_model_dependencies(kind: str) -> None:
    """Metadata-only compatibility check; never imports heavyweight inference modules."""
    packages = ("faster-whisper", "ctranslate2") if kind == "asr" else (
        "pyannote.audio", "torch", "torchaudio", "soundfile"
    )
    for package in packages:
        try:
            version = metadata.version(package)
        except metadata.PackageNotFoundError:
            raise SetupError("Install .[models] to process with the selected local model.") from None
        if package == "pyannote.audio" and not re.match(r"^3\.[0-9]+(?:\.|$)", version):
            raise SetupError("The selected diarization pipeline requires pyannote.audio 3.x.")


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
    def __init__(self, settings: Settings | Any):
        self.profile = settings

    def transcribe(self, path: Path) -> list[TranscriptSegment]:
        offline()
        if hasattr(self.profile, "model_identity"):
            if _profile_value(self.profile, "backend") != "faster-whisper":
                raise SetupError("The ASR profile requires a supported local backend.")
            artifact = _local_artifact(self.profile, "ASR")
            _validate_ct2_artifact(artifact)
            device = _profile_value(self.profile, "device")
            compute_type = _profile_value(self.profile, "compute_type")
        else:
            legacy_name = cast(str, _profile_value(self.profile, "asr_model"))
            artifact = Path(legacy_name).expanduser()
            if not artifact.is_dir():
                raise SetupError("The ASR model must be available as a local CTranslate2 model directory.")
            _validate_ct2_artifact(artifact)
            device = _profile_value(self.profile, "asr_device")
            compute_type = _profile_value(self.profile, "asr_compute_type")
        try:
            from faster_whisper import WhisperModel

            model = WhisperModel(
                str(artifact),
                device=str(device),
                compute_type=str(compute_type),
                local_files_only=True,
            )
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
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
    def __init__(self, settings: Settings | Any):
        self.profile = settings

    def diarize(self, path: Path) -> list[SpeakerTurn]:
        offline()
        if hasattr(self.profile, "model_identity"):
            if _profile_value(self.profile, "backend") != "pyannote":
                raise SetupError("The diarization profile requires a supported local backend.")
            artifact = _local_artifact(self.profile, "diarization")
            device = _profile_value(self.profile, "device")
        else:
            model_path = _profile_value(self.profile, "diarization_model")
            if not model_path:
                raise SetupError("Configure a complete local speaker-diarization-3.1 artifact before processing.")
            artifact = Path(str(model_path)).expanduser()
            if not artifact.is_dir():
                raise SetupError("The diarization model is unavailable locally. Configure a complete local model directory.")
            device = _profile_value(self.profile, "diarization_device")
        document = _validate_diarization_artifact(artifact)
        try:
            import soundfile as sf
            import torch
            from pyannote.audio import Pipeline
            yaml = importlib.import_module("yaml")
            # Pipeline.from_pretrained(3.x) accepts a config file; relative references
            # have already been resolved without changing the process working directory.
            with tempfile.TemporaryDirectory(prefix="meeting-pipeline-") as staging:
                localized = Path(staging) / "config.yaml"
                localized.write_text(yaml.safe_dump(document), encoding="utf-8")
                pipeline = Pipeline.from_pretrained(str(localized))
                try:
                    pipeline.to(torch.device(str(device)))
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
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
            raise SetupError(
                "Install .[models] and download all diarization weights before offline processing."
            ) from None


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
