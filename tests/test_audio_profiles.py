from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from meeting_protocol.audio import (
    FasterWhisperASR,
    PyannoteDiarizer,
    SetupError,
    _validate_diarization_artifact,
)
from meeting_protocol.profiles import ASRProfile, DiarizationProfile


def profile(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": "local-turbo",
        "label": "Whisper large v3 turbo",
        "model_identity": "openai/whisper-large-v3-turbo",
        "backend": "faster-whisper",
        "runtime_artifact": "",
        "device": "cpu",
        "compute_type": "int8",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_asr_loads_converted_local_artifact_and_keeps_word_alignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "ct2"
    artifact.mkdir()
    for name in ("config.json", "model.bin", "tokenizer.json", "preprocessor_config.json"):
        (artifact / name).write_text("{}", encoding="utf-8")
    observed: dict[str, object] = {}

    class Model:
        def __init__(self, model_path: str, **kwargs: object) -> None:
            observed.update(path=model_path, **kwargs)

        def transcribe(self, *_: object, **__: object) -> tuple[list[object], None]:
            words = [SimpleNamespace(start=0.1, end=0.4, word=" hello")]
            return [SimpleNamespace(start=0, end=1, text="hello", words=words)], None

    fake = ModuleType("faster_whisper")
    fake.WhisperModel = Model  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "faster_whisper", fake)
    monkeypatch.setattr("meeting_protocol.audio.release_gpu", lambda: None)

    custom = ASRProfile(id="operator-asr", label="Custom ASR", model_identity="operator/other",
                        compatible_slots=("asr",), availability="configured").runtime(str(artifact))
    result = FasterWhisperASR(custom).transcribe(tmp_path / "x.wav")

    assert observed == {
        "path": str(artifact),
        "device": "cpu",
        "compute_type": "int8",
        "local_files_only": True,
    }
    assert [(item.start, item.end, item.text) for item in result] == [(0.1, 0.4, " hello")]


def test_asr_rejects_remote_artifact_before_loading(tmp_path: Path) -> None:
    model = FasterWhisperASR(
        profile(model_identity="some/remote-model", runtime_artifact="org/remote-model")
    )
    with pytest.raises(SetupError, match="unavailable locally") as error:
        model.transcribe(tmp_path / "x.wav")
    assert str(tmp_path) not in str(error.value)


def test_asr_requires_local_converted_artifact(tmp_path: Path) -> None:
    model = FasterWhisperASR(profile(runtime_artifact="openai/whisper-large-v3-turbo"))
    with pytest.raises(SetupError, match="unavailable locally"):
        model.transcribe(tmp_path / "x.wav")


def test_diarization_config_rejects_remote_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text(
        "pipeline:\n  name: pyannote.audio.pipelines.SpeakerDiarization\n"
        "params:\n  segmentation: pyannote/segmentation-3.0\n",
        encoding="utf-8",
    )
    with pytest.raises(SetupError, match="references missing or remote"):
        _validate_diarization_artifact(tmp_path)


def test_diarization_requires_complete_local_config(tmp_path: Path) -> None:
    model = PyannoteDiarizer(
        profile(
            model_identity="pyannote/speaker-diarization-3.1",
            backend="pyannote",
            runtime_artifact=str(tmp_path),
        )
    )
    with pytest.raises(SetupError, match="include config.yaml"):
        model.diarize(tmp_path / "x.wav")


def test_diarizer_loads_local_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = tmp_path / "pipeline"
    artifact.mkdir()
    (artifact / "seg.bin").write_bytes(b"seg")
    (artifact / "embed.bin").write_bytes(b"embedding")
    (artifact / "config.yaml").write_text(
        "pipeline:\n  name: local.pipeline\n  params:\n"
        "    segmentation: ./seg.bin\n    embedding: ./embed.bin\n", encoding="utf-8"
    )
    loaded: list[str] = []
    localized: list[dict[str, object]] = []

    class Array:
        def __init__(self) -> None:
            self.T = self

        def copy(self) -> "Array":
            return self

    class Annotation:
        def itertracks(self, *, yield_label: bool) -> list[object]:
            assert yield_label
            return []

    class Pipeline:
        @staticmethod
        def from_pretrained(config_path: str) -> "Pipeline":
            import yaml

            assert Path(config_path).is_file()
            localized.append(yaml.safe_load(Path(config_path).read_text(encoding="utf-8")))
            loaded.append(config_path)
            return Pipeline()

        def to(self, _: object) -> None:
            return None

        def __call__(self, _: object) -> SimpleNamespace:
            return SimpleNamespace(speaker_diarization=Annotation())

    torch = ModuleType("torch")
    torch.device = lambda value: value  # type: ignore[attr-defined]
    torch.from_numpy = lambda value: value  # type: ignore[attr-defined]
    soundfile = ModuleType("soundfile")
    soundfile.read = lambda *args, **kwargs: (Array(), 16_000)  # type: ignore[attr-defined]
    pyannote = ModuleType("pyannote")
    pyannote.__path__ = []  # type: ignore[attr-defined]
    pyannote_audio = ModuleType("pyannote.audio")
    pyannote_audio.Pipeline = Pipeline  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "torch", torch)
    monkeypatch.setitem(__import__("sys").modules, "soundfile", soundfile)
    monkeypatch.setitem(__import__("sys").modules, "pyannote", pyannote)
    monkeypatch.setitem(__import__("sys").modules, "pyannote.audio", pyannote_audio)
    monkeypatch.setattr("meeting_protocol.audio.release_gpu", lambda: None)

    legacy = DiarizationProfile(id="legacy-diarization", label="Legacy diarization",
                                model_identity="unknown (legacy DIARIZATION_MODEL)",
                                compatible_slots=("diarization",), availability="configured").runtime(str(artifact))
    result = PyannoteDiarizer(legacy).diarize(tmp_path / "x.wav")

    assert len(loaded) == 1 and loaded[0] != str(artifact / "config.yaml")
    assert localized[0]["pipeline"]["params"] == {  # type: ignore[index]
        "segmentation": str(artifact / "seg.bin"), "embedding": str(artifact / "embed.bin")
    }
    assert not Path(loaded[0]).exists()
    assert result == []


@pytest.mark.parametrize("segmentation,embedding", [
    ("pyannote/segmentation-3.0", "./embed.bin"),
    ("remote", "./embed.bin"), ("./missing.bin", "./embed.bin"),
    ("./seg.bin", "./missing.bin"), ("./seg.bin", {"checkpoint_path": ["bad"]}),
])
def test_diarization_rejects_missing_remote_and_nested_refs(tmp_path, segmentation, embedding):
    import yaml

    (tmp_path / "seg.bin").write_bytes(b"seg")
    (tmp_path / "embed.bin").write_bytes(b"embed")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"pipeline": {
        "name": "pyannote.audio.pipelines.SpeakerDiarization",
        "params": {"segmentation": segmentation, "embedding": embedding},
    }}), encoding="utf-8")
    with pytest.raises(SetupError, match="missing or remote"):
        _validate_diarization_artifact(tmp_path)


def test_complete_diarization_absolute_refs(tmp_path):
    import yaml

    for name in ("seg.bin", "embed.bin"):
        (tmp_path / name).write_bytes(b"weights")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"pipeline": {
        "name": "pyannote.audio.pipelines.SpeakerDiarization",
        "params": {"segmentation": str(tmp_path / "seg.bin"),
                   "embedding": {"checkpoint_path": str(tmp_path / "embed.bin")}},
    }}), encoding="utf-8")
    assert _validate_diarization_artifact(tmp_path)["pipeline"]["params"]["segmentation"] == str(tmp_path / "seg.bin")
