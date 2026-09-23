"""Offline artifact checks adapted from origin/main b2f9eb8."""
import importlib
import re
from importlib import metadata
from pathlib import Path
from typing import Any


class SetupError(RuntimeError):
    pass


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
        raise SetupError("Install backend/requirements-gpu.txt to validate the local diarization config before processing.") from None
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
            raise SetupError("Install backend/requirements-gpu.txt to process with the selected local model.") from None
        if package == "pyannote.audio" and not re.match(r"^3\.[0-9]+(?:\.|$)", version):
            raise SetupError("The selected diarization pipeline requires pyannote.audio 3.x.")
