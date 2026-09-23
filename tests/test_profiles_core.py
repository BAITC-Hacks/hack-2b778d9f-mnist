import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from meeting_protocol.config import Settings
from meeting_protocol.models import Meeting, ModelSelection, ProcessingStatus
from meeting_protocol.profiles import AttemptSnapshot, ProfileCatalog, ProfileError
from meeting_protocol.store import Store


def configured_catalog(tmp_path, **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    asr = tmp_path / "asr"
    asr.mkdir()
    for name in ("config.json", "model.bin", "tokenizer.json", "preprocessor_config.json"):
        (asr / name).write_text("{}")
    diar = tmp_path / "diar"
    diar.mkdir()
    for name in ("segmentation.bin", "embedding.bin"):
        (diar / name).write_bytes(b"weights")
    (diar / "config.yaml").write_text(
        "pipeline:\n  name: pyannote.audio.pipelines.SpeakerDiarization\n  params:\n"
        f"    segmentation: '{diar / 'segmentation.bin'}'\n"
        f"    embedding: '{diar / 'embedding.bin'}'\n", encoding="utf-8")
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"gguf")
    settings = Settings(_env_file=None, data_root=tmp_path / "data", asr_model_artifact=asr,
                        diarization_model_artifact=diar, model_path=gguf, **kwargs)
    return settings


def test_root_anchored_default_model_path(tmp_path):
    settings = Settings(_env_file=None, data_root=tmp_path / "data")
    assert settings.model_path == Settings.model_fields["model_path"].default
    snapshot = ProfileCatalog.from_settings(settings).resolve(ModelSelection())
    assert snapshot.bindings.diarization == "pyannote-3.1-local"


def test_builtin_selection_and_allowlisted_catalog(tmp_path):
    catalog = ProfileCatalog.from_settings(configured_catalog(tmp_path))
    snapshot = catalog.resolve(ModelSelection())
    assert snapshot.bindings.asr == "whisper-turbo-local"
    assert snapshot.bindings.diarization == "pyannote-3.1-local"
    assert snapshot.profile_for("diarization").availability == "configured"
    assert snapshot.profile_for("extract_tasks").model_identity == "Qwen3.5-4B-UD-Q6_K_XL"
    public = catalog.public_catalog()
    assert set(public) == {"schema_version", "defaults", "profiles"}
    assert public["schema_version"] == 1
    assert "_artifact" not in json.dumps(public) and str(tmp_path) not in json.dumps(public)
    assert set(public["profiles"][0]) <= {"id", "label", "model_identity", "operations", "availability", "reason_code"}
    assert public["profiles"][0]["availability"] == "configured"


def test_invalid_selection_and_legacy_identity_not_relabelled(tmp_path):
    catalog = ProfileCatalog.from_settings(configured_catalog(tmp_path))
    with pytest.raises(ProfileError):
        catalog.resolve(ModelSelection(asr="not-a-profile"))
    settings = configured_catalog(tmp_path / "legacy", asr_model="my-private-asr")
    catalog = ProfileCatalog.from_settings(settings)
    assert catalog.defaults["asr"] == "legacy-asr"
    assert catalog.profiles["legacy-asr"].model_identity.startswith("unknown")
    assert catalog.profiles["legacy-asr"].id != "whisper-turbo-local"


def test_explicit_defaults_override_legacy_and_missing_is_preflight_error(tmp_path):
    settings = configured_catalog(tmp_path, asr_model="C:/legacy/local-asr",
                                  model_default_bindings_json='{"asr":"whisper-turbo-local"}')
    catalog = ProfileCatalog.from_settings(settings)
    assert catalog.defaults["asr"] == "whisper-turbo-local"
    settings.asr_model_artifact = tmp_path / "absent-ct2"
    catalog = ProfileCatalog.from_settings(settings)
    snapshot = catalog.resolve(ModelSelection(asr="whisper-turbo-local"))
    with pytest.raises(ProfileError, match="CTranslate2"):
        import asyncio
        asyncio.run(catalog.preflight(snapshot))


def test_per_operation_llm_bindings_and_attempt_immutability(tmp_path):
    settings = configured_catalog(tmp_path)
    # IDs cannot alias a different GGUF merely by changing the profile name.
    profiles = json.loads(settings.model_profiles_json or "[]")
    profiles.extend([
        {"kind": "llm", "id": "alt-a", "label": "Alt A", "model_identity": "other-a",
         "operations": ["extract_tasks"], "artifact_path": str(tmp_path / "model.gguf"), "base_url": settings.llm_base_url,
         "served_alias": "alias-a", "max_output_tokens": 2048,
         "context_size": 8192, "temperature": 0.1, "timeout_seconds": 1},
        {"kind": "llm", "id": "alt-b", "label": "Alt B", "model_identity": "other-b",
         "operations": ["verify_tasks"], "artifact_path": str(tmp_path / "model.gguf"), "base_url": settings.llm_base_url,
         "served_alias": "alias-b", "max_output_tokens": 2048,
         "context_size": 8192, "temperature": 0.1, "timeout_seconds": 1},
    ])
    settings.model_profiles_json = json.dumps(profiles)
    catalog = ProfileCatalog.from_settings(settings)
    with pytest.raises(ProfileError, match="different aliases"):
        catalog.resolve(ModelSelection(extract_tasks="alt-a", verify_tasks="alt-b"))


@pytest.mark.parametrize("bad_url", ["https://example.com/v1", "http://user@127.0.0.1/v1", "http://127.0.0.1/v1?x=1", "http://127.0.0.1/a/../v1", "http://localhost/v1", "http://127.0.0.2/v1"])
def test_custom_profile_rejects_unsafe_endpoints(tmp_path, bad_url):
    settings = configured_catalog(tmp_path)
    settings.model_profiles_json = json.dumps([{
        "kind": "llm", "id": "custom-llm", "label": "Custom", "model_identity": "user/model",
        "operations": ["extract_tasks"], "artifact_path": str(tmp_path / "model.gguf"),
        "base_url": bad_url, "served_alias": "alias",
    }])
    with pytest.raises(ProfileError):
        ProfileCatalog.from_settings(settings)


def test_custom_profile_runtime_is_private_and_missing_artifact_resolves(tmp_path):
    settings = configured_catalog(tmp_path)
    settings.model_profiles_json = json.dumps([{
        "kind": "asr", "id": "operator-asr", "label": "Operator ASR", "model_identity": "org/model",
        "operations": ["asr"], "artifact_path": str(tmp_path / "not-here"),
    }])
    catalog = ProfileCatalog.from_settings(settings)
    snapshot = catalog.resolve(ModelSelection(asr="operator-asr"))
    assert snapshot.profile_for("asr").runtime_artifact == str(tmp_path / "not-here")
    assert catalog.public_catalog()["profiles"][-1]["availability"] == "missing_files"


def test_runtime_is_frozen_real_field(tmp_path):
    snapshot = ProfileCatalog.from_settings(configured_catalog(tmp_path)).resolve(ModelSelection())
    selected = snapshot.profile_for("asr")
    assert selected.model_dump()["runtime_artifact"] == str(tmp_path / "asr")
    with pytest.raises(ValidationError):
        selected.runtime_artifact = "other"
    with pytest.raises((AttributeError, ValidationError, TypeError)):
        selected.__setattr__("_artifact", "other")
    with pytest.raises(ValidationError):
        snapshot.bindings.asr = "other"
    with pytest.raises(ValidationError):
        AttemptSnapshot.model_validate({**snapshot.model_dump(), "surprise": 1})
    assert selected.runtime(str(tmp_path / "asr")) is not selected


@pytest.mark.parametrize("field,value", [
    ("label", "C:/sensitive/operator"), ("model_identity", "https://private.example/model"),
    ("label", "sk-secretkey"), ("model_identity", "Bearer abc"), ("label", "bad\nname"),
    ("label", "../private/secret.gguf"), ("model_identity", "../private/secret.gguf"),
    ("label", "./private/secret.gguf"), ("model_identity", "org/../secret.gguf"),
])
def test_public_metadata_rejects_secrets(tmp_path, field, value):
    settings = configured_catalog(tmp_path)
    entry = {"kind": "asr", "id": "operator-asr", "label": "safe", "model_identity": "org/model",
             "operations": ["asr"], "artifact_path": str(tmp_path / "asr")}
    entry[field] = value
    settings.model_profiles_json = json.dumps([entry])
    with pytest.raises(ProfileError, match="configuration is invalid") as error:
        ProfileCatalog.from_settings(settings)
    assert value not in str(error.value)


@pytest.mark.parametrize("binding", ["unknown-id", "qwen-4b-local", 123, None, "BAD_ID"])
def test_defaults_validate_id_type_and_slot(tmp_path, binding):
    settings = configured_catalog(tmp_path)
    settings.model_default_bindings_json = json.dumps({"asr": binding})
    with pytest.raises(ProfileError, match="bindings are invalid"):
        ProfileCatalog.from_settings(settings)


@pytest.mark.parametrize("context,output,temperature,timeout", [
    (4096, 2048, 0.1, 120), (8192, 2048, float("nan"), 120),
    (8192, 2048, 0.1, float("inf")), (8192, 2048, -1, 120),
    (8192, 2048, 0.1, 0), (999999, 2048, 0.1, 120),
])
def test_custom_llm_budget_validation(tmp_path, context, output, temperature, timeout):
    settings = configured_catalog(tmp_path)
    settings.model_profiles_json = json.dumps([{
        "kind": "llm", "id": "custom-llm", "label": "Local model", "model_identity": "org/model",
        "operations": ["extract_tasks"], "artifact_path": str(tmp_path / "model.gguf"),
        "base_url": settings.llm_base_url, "served_alias": "alias",
        "context_size": context, "max_output_tokens": output,
        "temperature": temperature, "timeout_seconds": timeout,
    }])
    with pytest.raises(ProfileError, match="configuration is invalid"):
        ProfileCatalog.from_settings(settings)


def test_preflight_checks_lightweight_metadata_without_importing_models(tmp_path, monkeypatch):
    catalog = ProfileCatalog.from_settings(configured_catalog(tmp_path))
    snapshot = catalog.resolve(ModelSelection())
    monkeypatch.setattr("meeting_protocol.audio.metadata.version", lambda package: "3.4.0" if package == "pyannote.audio" else "1.0.0")
    original_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=(
            {"data": [{"id": "qwen3.5-4b-local"}]} if request.url.path == "/v1/models" else
            {"model_path": str(tmp_path / "model.gguf")})))
        return original_client(transport=transport, **kwargs)

    monkeypatch.setattr("meeting_protocol.profiles.httpx.AsyncClient", fake_client)
    asyncio.run(catalog.preflight(snapshot))


@pytest.mark.parametrize("properties,code", [
    ({}, "identity_unverified"), ({"model_path": 3}, "identity_unverified"),
    ({"model_path": "other.gguf"}, "identity_unverified"),
    ({"model_path": "C:/secret/other.gguf"}, "identity_unverified"),
    ({"model_path": "none"}, "identity_unverified"),
])
def test_llm_props_must_match_artifact(tmp_path, monkeypatch, properties, code):
    catalog = ProfileCatalog.from_settings(configured_catalog(tmp_path))
    snapshot = catalog.resolve(ModelSelection())
    monkeypatch.setattr("meeting_protocol.audio.metadata.version", lambda name: "3.4" if name == "pyannote.audio" else "1")
    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=(
            {"data": [{"id": "qwen3.5-4b-local"}]} if request.url.path == "/v1/models" else properties)))
        return original_client(transport=transport, **kwargs)

    monkeypatch.setattr("meeting_protocol.profiles.httpx.AsyncClient", client)
    with pytest.raises(ProfileError) as error:
        asyncio.run(catalog.preflight(snapshot))
    assert error.value.code == code
    assert str(tmp_path) not in str(error.value)


@pytest.mark.parametrize("response", [None, {}, {"data": [42]}, {"data": [{"id": "wrong"}]}])
def test_llm_alias_malformed_or_absent_fails_closed(tmp_path, monkeypatch, response):
    catalog = ProfileCatalog.from_settings(configured_catalog(tmp_path))
    snapshot = catalog.resolve(ModelSelection())
    monkeypatch.setattr("meeting_protocol.audio.metadata.version", lambda name: "3.4" if name == "pyannote.audio" else "1")
    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response))
        return original_client(transport=transport, **kwargs)

    monkeypatch.setattr("meeting_protocol.profiles.httpx.AsyncClient", client)
    with pytest.raises(ProfileError) as error:
        asyncio.run(catalog.preflight(snapshot))
    assert error.value.code in {"identity_unverified", "server_alias_missing"}


def test_missing_model_dependencies_fail_before_llm_probe(tmp_path, monkeypatch):
    from importlib import metadata

    catalog = ProfileCatalog.from_settings(configured_catalog(tmp_path))

    def missing(_: str):
        raise metadata.PackageNotFoundError("missing")

    monkeypatch.setattr("meeting_protocol.audio.metadata.version", missing)
    with pytest.raises(ProfileError) as error:
        asyncio.run(catalog.preflight(catalog.resolve(ModelSelection())))
    assert error.value.code == "incompatible" and error.value.slot == "asr"


def test_pyannote_non_3x_rejected_without_importing_inference(tmp_path, monkeypatch):
    catalog = ProfileCatalog.from_settings(configured_catalog(tmp_path))
    monkeypatch.setattr("meeting_protocol.audio.metadata.version", lambda name: "4.0.0" if name == "pyannote.audio" else "1.0.0")
    with pytest.raises(ProfileError) as error:
        asyncio.run(catalog.preflight(catalog.resolve(ModelSelection())))
    assert error.value.code == "incompatible" and error.value.slot == "diarization"


def test_model_selection_rejects_invalid_slug():
    for invalid in ("../../private", "foo..bar", "foo/secret", "foo.", ".foo"):
        with pytest.raises(ValidationError):
            ModelSelection(asr=invalid)
    assert ModelSelection(diarization="pyannote-3.1-local").diarization == "pyannote-3.1-local"


def test_attempt_snapshot_readback_and_source_immutability(tmp_path):
    settings = configured_catalog(tmp_path)
    snapshot = ProfileCatalog.from_settings(settings).resolve(ModelSelection())
    store = Store(tmp_path / "app.db")
    meeting = Meeting(title="Attempt")
    store.save(meeting)
    source = tmp_path / "audio.wav"
    source.write_bytes(b"first")
    store.attach_audio(meeting.id, source)
    attempt = store.admit_attempt(meeting.id, snapshot, ModelSelection())
    with store.connect() as db:
        immutable_before = db.execute("SELECT snapshot_json, source_audio FROM processing_attempts WHERE id=?", (attempt.id,)).fetchone()
    reopened = Store(tmp_path / "app.db").get_attempt(attempt.id)
    assert reopened.source_audio == source
    assert reopened.snapshot.profile_for("asr")._artifact == str(tmp_path / "asr")
    assert reopened.snapshot.bindings == snapshot.bindings
    store.stage_attempt(attempt.id, ProcessingStatus.normalizing)
    store.finish_attempt(attempt.id, Meeting(title="Attempt result"), error="expected failure")
    with store.connect() as db:
        immutable_after = db.execute("SELECT snapshot_json, source_audio FROM processing_attempts WHERE id=?", (attempt.id,)).fetchone()
    assert immutable_after == immutable_before
    assert [item.id for item in store.list_attempts(meeting.id)] == [attempt.id]


def test_attach_audio_checks_stale_and_active(tmp_path):
    store = Store(tmp_path / "app.db")
    meeting = Meeting(title="Upload")
    store.save(meeting)
    path = tmp_path / "audio.wav"
    with pytest.raises(ValueError, match="changed"):
        store.attach_audio(meeting.id, path, expected_body="stale")
    attached = store.attach_audio(meeting.id, path, expected_body=meeting.model_dump_json())
    assert attached.status.value == "uploaded" and store.audio(meeting.id) == path.resolve()
    attached.status = ProcessingStatus.queued
    attached.active_attempt_id = "active"
    store.save(attached)
    with pytest.raises(ValueError, match="processing"):
        store.attach_audio(meeting.id, path)


def test_recovery_does_not_mutate_attempt_snapshot_or_source(tmp_path):
    settings = configured_catalog(tmp_path)
    snapshot = ProfileCatalog.from_settings(settings).resolve(ModelSelection())
    store = Store(tmp_path / "recover.db")
    meeting = Meeting(title="Recovery")
    store.save(meeting)
    source = tmp_path / "restart.wav"
    source.write_bytes(b"audio")
    store.attach_audio(meeting.id, source)
    attempt = store.admit_attempt(meeting.id, snapshot, ModelSelection())
    with store.connect() as db:
        before = db.execute("SELECT snapshot_json, source_audio FROM processing_attempts WHERE id=?", (attempt.id,)).fetchone()
    store.recover()
    with store.connect() as db:
        after = db.execute("SELECT snapshot_json, source_audio FROM processing_attempts WHERE id=?", (attempt.id,)).fetchone()
    assert after == before
