"""Check local artifacts and the llama.cpp server without loading speech models."""
import importlib.metadata
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.audio import _local_model
from app.model_artifacts import _validate_ct2_artifact, _validate_diarization_artifact, check_model_dependencies
from app.extract import check_llama_server


def main():
    checks = {name: bool(shutil.which(name)) for name in ("ffmpeg", "ffprobe")}
    versions = {}
    for name in ("fastapi", "torch", "torchaudio", "faster-whisper", "ctranslate2", "pyannote.audio", "PyYAML"):
        try:
            versions[name] = importlib.metadata.version(name)
            checks[name] = True
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
            checks[name] = False
    errors = {}
    for kind, variable, validate in (
        ("asr", "ASR_MODEL_ARTIFACT", _validate_ct2_artifact),
        ("diarization", "DIARIZATION_MODEL_ARTIFACT", _validate_diarization_artifact),
    ):
        try:
            validate(_local_model(variable))
            check_model_dependencies(kind)
            checks[kind] = True
        except Exception as error:
            checks[kind] = False
            errors[kind] = str(error)
    try:
        check_llama_server()
        checks["llama-server"] = True
    except Exception as error:
        checks["llama-server"] = False
        errors["llama-server"] = str(error)
    print(json.dumps({"ready": all(checks.values()), "checks": checks, "versions": versions,
                      "errors": errors, "optional_pdf": bool(shutil.which("libreoffice")),
                      "note": "Artifact and server-reported path checks only; real inference is not verified."}, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
