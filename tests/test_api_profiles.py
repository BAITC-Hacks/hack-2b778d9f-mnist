import asyncio

import pytest
from fastapi.testclient import TestClient

from meeting_protocol.api import create_app
from meeting_protocol.config import Settings
from meeting_protocol.models import ProcessingStatus
from meeting_protocol.profiles import (
    ASRProfile,
    DiarizationProfile,
    LLMProfile,
    ProfileCatalog,
    ProfileError,
)


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, public_host="127.0.0.1", agent_api_key="",
                    data_root=tmp_path / "data", database_path=tmp_path / "app.db")


class IdleProcessor:
    def __init__(self, settings, store):
        self.catalog = ProfileCatalog.__new__(ProfileCatalog)
        self.catalog.settings = settings
        self.catalog.defaults = {slot: ("whisper-turbo-local" if slot == "asr" else
                                        "diar-local" if slot == "diarization" else "qwen-4b-local")
                                 for slot in ("asr", "diarization", "extract_tasks", "verify_tasks",
                                              "resolve_speakers", "generate_summary")}
        self.catalog.profiles = {
            "whisper-turbo-local": ASRProfile(id="whisper-turbo-local", label="ASR", model_identity="asr",
                compatible_slots=("asr",), availability="configured"),
            "diar-local": DiarizationProfile(id="diar-local", label="Diar", model_identity="diarization",
                compatible_slots=("diarization",), availability="configured"),
            "qwen-4b-local": LLMProfile(id="qwen-4b-local", label="LLM", model_identity="llm",
                compatible_slots=("extract_tasks", "verify_tasks", "resolve_speakers", "generate_summary"),
                availability="configured"),
        }
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, attempt_id):
        self.started.set()
        await self.release.wait()


def make_app(settings):
    app = create_app(settings, IdleProcessor)
    catalog = app.state.processor.catalog
    # Exercise the API without model downloads or a local model server.

    async def preflight(snapshot):
        return None

    catalog.preflight = preflight
    return app


def test_catalog_and_selection_are_public_and_saved(settings):
    app = make_app(settings)
    with TestClient(app) as client:
        catalog = client.get("/api/v1/model-profiles")
        assert catalog.status_code == 200
        payload = catalog.json()
        assert "defaults" in payload and payload["profiles"]
        assert all(not {"_runtime", "runtime_artifact", "path", "url", "token"}.intersection(item)
                   for item in payload["profiles"])
        made = client.post("/api/v1/meetings", json={"title": "Demo"})
        assert made.status_code == 201
        mid = made.json()["id"]
        assert client.post(f"/api/v1/meetings/{mid}/audio",
                           files={"file": ("source.wav", b"audio")}).status_code == 200
        changed = client.patch(f"/api/v1/meetings/{mid}/model-selection",
                               json={"model_selection": {"asr": "whisper-turbo-local"}})
        assert changed.status_code == 200
        assert changed.json()["model_selection"]["asr"] == "whisper-turbo-local"
        assert changed.json()["attempts"] == []
        assert app.state.store.get(mid).status == ProcessingStatus.uploaded


def test_invalid_selection_does_not_echo_request_secrets(settings):
    with TestClient(make_app(settings)) as client:
        secret = "https://private.example/token?secret=never-reflect"
        response = client.post("/api/v1/meetings", json={
            "title": "Private", "model_selection": {"asr": secret},
        })
        assert response.status_code == 422
        assert secret not in response.text and "private.example" not in response.text


def test_process_admits_one_attempt_and_returns_only_safe_metadata(settings):
    app = make_app(settings)
    with TestClient(app) as client:
        mid = client.post("/api/v1/meetings", json={"title": "Demo"}).json()["id"]
        upload = client.post(f"/api/v1/meetings/{mid}/audio",
                             files={"file": ("secret-path.wav", b"source")})
        assert upload.status_code == 200
        response = client.post(f"/api/v1/meetings/{mid}/process", json={})
        assert response.status_code == 202
        queued = response.json()
        assert queued["status"] == "queued" and queued["attempt_id"]
        assert "bindings" in queued and "source" not in response.text
        again = client.post(f"/api/v1/meetings/{mid}/process", json={})
        assert again.status_code == 409
        detail = client.get(f"/api/v1/meetings/{mid}").json()
        assert detail["attempts"][0]["attempt_id"] == queued["attempt_id"]
        assert detail["attempts"][0]["bindings"] == queued["bindings"]
        assert "secret-path" not in str(detail)
        app.state.processor.release.set()


def test_profile_validation_errors_are_sanitized(settings):
    with TestClient(make_app(settings)) as client:
        mid = client.post("/api/v1/meetings", json={"title": "Demo"}).json()["id"]
        secret = "C:\\private\\path\\token"
        response = client.patch(f"/api/v1/meetings/{mid}/model-selection",
                                json={"model_selection": {"unexpected": secret}})
        assert response.status_code == 422
        assert secret not in response.text and "private" not in response.text


def test_process_saved_selection_can_be_cleared(settings):
    app = make_app(settings)
    with TestClient(app) as client:
        mid = client.post("/api/v1/meetings", json={"title": "Demo"}).json()["id"]
        client.post(f"/api/v1/meetings/{mid}/audio", files={"file": ("input.wav", b"audio")})
        saved = client.patch(f"/api/v1/meetings/{mid}/model-selection",
                             json={"model_selection": {"asr": "whisper-turbo-local"}})
        assert saved.status_code == 200
        queued = client.post(f"/api/v1/meetings/{mid}/process", json={"model_selection": {}})
        assert queued.status_code == 202
        assert app.state.store.get(mid).model_selection.asr is None
        app.state.processor.release.set()


def test_http_preflight_failure_does_not_admit_or_reflect_private_error(settings):
    app = make_app(settings)

    async def fail(snapshot):
        raise ProfileError("missing_files", "asr", "C:\\private\\models\\secret")

    app.state.processor.catalog.preflight = fail
    with TestClient(app) as client:
        mid = client.post("/api/v1/meetings", json={"title": "Demo"}).json()["id"]
        client.post(f"/api/v1/meetings/{mid}/audio", files={"file": ("input.wav", b"audio")})
        response = client.post(f"/api/v1/meetings/{mid}/process")
        assert response.status_code == 503 and "private" not in response.text
        assert app.state.store.list_attempts(mid) == []
        assert app.state.store.get(mid).status == ProcessingStatus.uploaded
