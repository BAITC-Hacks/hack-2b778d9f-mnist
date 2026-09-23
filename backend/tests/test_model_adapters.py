import json
from io import BytesIO
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import yaml

from app.audio import diarize_turns
from app.extract import SCHEMA, check_llama_server, llama_chat, llm_base_url
from app.model_artifacts import SetupError, _validate_ct2_artifact, _validate_diarization_artifact


def test_transformers_snapshot_is_not_a_ct2_artifact(tmp_path):
    for name in ("config.json", "model.safetensors", "tokenizer.json", "preprocessor_config.json"):
        (tmp_path / name).touch()
    with pytest.raises(SetupError, match="CTranslate2"):
        _validate_ct2_artifact(tmp_path)


def pipeline_config(tmp_path, segmentation="segmentation.bin"):
    for name in ("segmentation.bin", "embedding.bin"):
        (tmp_path / name).touch()
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"pipeline": {
        "name": "pyannote.audio.pipelines.SpeakerDiarization",
        "params": {"segmentation": segmentation, "embedding": "embedding.bin"},
    }}))


def test_remote_diarization_weights_are_rejected(tmp_path):
    pipeline_config(tmp_path, "pyannote/segmentation-3.0")
    with pytest.raises(SetupError, match="missing or remote"):
        _validate_diarization_artifact(tmp_path)


def test_pyannote_3_loads_localized_config_and_returns_speaker_turns(tmp_path, short_wav, monkeypatch):
    pipeline_config(tmp_path)
    original = (tmp_path / "config.yaml").read_bytes()
    monkeypatch.setenv("DIARIZATION_MODEL_ARTIFACT", str(tmp_path))
    monkeypatch.setattr("app.audio.check_model_dependencies", lambda _: None)
    class Pipeline:
        @staticmethod
        def from_pretrained(config):
            params = yaml.safe_load(Path(config).read_text())["pipeline"]["params"]
            assert params["segmentation"] == str(tmp_path / "segmentation.bin")
            assert params["embedding"] == str(tmp_path / "embedding.bin")
            return Pipeline()

        def to(self, device):
            assert device == "cpu"

        def __call__(self, audio):
            assert audio["sample_rate"] == 16000
            return SimpleNamespace(itertracks=lambda **_: iter([
                (SimpleNamespace(start=0.1, end=0.8), None, "speaker_0"),
            ]))

    monkeypatch.setitem(sys.modules, "pyannote.audio", SimpleNamespace(Pipeline=Pipeline))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        device=lambda value: value, cuda=SimpleNamespace(is_available=lambda: False),
        from_numpy=lambda _: SimpleNamespace(unsqueeze=lambda _: "waveform"),
    ))
    assert diarize_turns(short_wav) == [(0.1, 0.8, "speaker_0")]
    assert (tmp_path / "config.yaml").read_bytes() == original


@pytest.mark.parametrize("url", ["https://127.0.0.1/v1", "http://example.com/v1",
                                 "http://user@127.0.0.1/v1", "http://127.0.0.1/v1?key=x"])
def test_llama_endpoint_rejects_nonlocal_or_credential_urls(url, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", url)
    with pytest.raises(ValueError, match="127.0.0.1"):
        llm_base_url()


@pytest.mark.parametrize("finish", ["stop", "length"])
def test_llama_structured_request_and_truncation(monkeypatch, finish):
    monkeypatch.setattr("app.extract.check_llama_server", lambda: None)
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:27362/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen3.5-4b-local")
    def request(req, **kwargs):
        assert req.full_url == "http://127.0.0.1:27362/v1/chat/completions"
        body = json.loads(req.data)
        assert body["model"] == "qwen3.5-4b-local"
        assert body["response_format"]["json_schema"]["schema"] == SCHEMA
        return BytesIO(json.dumps({"choices": [{"finish_reason": finish, "message": {
            "content": '<think>private reasoning</think>{"summary":"Ready","actions":[]}',
        }}]}).encode())
    monkeypatch.setattr("app.extract.build_opener", lambda *args: SimpleNamespace(open=request))
    if finish == "stop":
        assert llama_chat([], SCHEMA) == {"summary": "Ready", "actions": []}
    else:
        with pytest.raises(ValueError, match="did not complete"):
            llama_chat([], SCHEMA)


@pytest.mark.parametrize("matches", [True, False])
def test_llama_preflight_checks_served_artifact(tmp_path, monkeypatch, matches):
    artifact = tmp_path / "Qwen.gguf"
    artifact.touch()
    monkeypatch.setenv("MODEL_PATH", str(artifact))
    monkeypatch.setenv("LLM_MODEL", "qwen3.5-4b-local")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:27362/v1")
    def request(url, **kwargs):
        if url.endswith("/models"):
            result = {"data": [{"id": "qwen3.5-4b-local"}]}
        else:
            assert url == "http://127.0.0.1:27362/props"
            result = {"model_path": str(artifact if matches else tmp_path / "other.gguf")}
        return BytesIO(json.dumps(result).encode())
    monkeypatch.setattr("app.extract.build_opener", lambda *args: SimpleNamespace(open=request))
    if matches:
        check_llama_server()
    else:
        with pytest.raises(RuntimeError, match="does not match"):
            check_llama_server()
