import asyncio
import json
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from meeting_protocol.api import create_app
from meeting_protocol.config import Settings
from meeting_protocol.models import Meeting, ModelSelection, ProcessingStatus, ResolvedBindings
from meeting_protocol.processor import Processor
from meeting_protocol.profiles import ASRProfile, AttemptSnapshot, DiarizationProfile, LLMProfile
from meeting_protocol.runtime_lock import RuntimeLock
from meeting_protocol.store import Store


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, public_host="127.0.0.1", agent_api_key="",
                    data_root=tmp_path / "data", database_path=tmp_path / "app.db")


def snapshot(source):
    bindings = ResolvedBindings(asr="asr-local", diarization="diar-local",
                                extract_tasks="llm-local", verify_tasks="llm-local",
                                resolve_speakers="llm-local", generate_summary="llm-local")
    return AttemptSnapshot(bindings=bindings, profiles=(
        ASRProfile(id="asr-local", label="ASR", model_identity="whisper", compatible_slots=("asr",),
                   availability="configured", runtime_artifact=str(source)),
        DiarizationProfile(id="diar-local", label="Diar", model_identity="diarization",
                           compatible_slots=("diarization",), availability="configured",
                           runtime_artifact=str(source)),
        LLMProfile(id="llm-local", label="LLM", model_identity="llm",
                   compatible_slots=("extract_tasks", "verify_tasks", "resolve_speakers", "generate_summary"),
                   availability="configured", runtime_artifact=str(source)),
    ))


def admitted(settings):
    store = Store(settings.database_path)
    meeting = Meeting(title="test")
    store.save(meeting)
    source = settings.database_path.parent / "input.wav"
    source.write_bytes(b"original")
    store.attach_audio(meeting.id, source)
    attempt = store.admit_attempt(meeting.id, snapshot(source), ModelSelection(),
                                  expected_body=store.get(meeting.id).model_dump_json(),
                                  expected_audio=source)
    return store, meeting, source, attempt


def test_atomic_claim_only_once_and_stale_finish_cannot_override(settings):
    store, meeting, _, attempt = admitted(settings)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(store.claim_attempt, [attempt.id, attempt.id]))
    assert sum(claim is not None for claim in claims) == 1
    assert store.get(meeting.id).status == ProcessingStatus.normalizing
    assert store.stage_attempt(attempt.id, ProcessingStatus.transcribing)
    assert store.finish_attempt(attempt.id, Meeting(title="result"))
    assert not store.finish_attempt(attempt.id, Meeting(title="stale"))
    assert store.get(meeting.id).title == "test"


def test_snapshot_roundtrip_and_sql_provenance_immutable(settings):
    store, _, source, attempt = admitted(settings)
    assert store.get_attempt(attempt.id).snapshot.profiles[0].runtime_artifact == str(source)
    with store.connect() as db:
        raw = db.execute("SELECT snapshot_json FROM processing_attempts WHERE id=?", (attempt.id,)).fetchone()[0]
        assert "runtime_artifact" in raw
        for column, value in (("snapshot_json", "{}"), ("source_audio", "changed"),
                              ("meeting_id", "changed")):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(f"UPDATE processing_attempts SET {column}=? WHERE id=?",
                           (value, attempt.id))
    old = json.loads(raw)
    for profile in old["profiles"]:
        profile["_runtime"] = {"artifact": profile.pop("runtime_artifact"),
                               "url": profile.get("base_url", "")}
    assert Store._snapshot_read(json.dumps(old)).profiles[0].runtime_artifact == str(source)


def test_audio_replacement_rejects_stale_body_or_source_and_preserves_original(settings):
    store = Store(settings.database_path)
    meeting = Meeting(title="source")
    store.save(meeting)
    first, second, third = (settings.database_path.parent / f"{name}.wav"
                            for name in ("first", "second", "third"))
    for path in (first, second, third):
        path.write_bytes(b"original")
    body = store.get(meeting.id).model_dump_json()
    store.attach_audio(meeting.id, first, expected_body=body)
    body = store.get(meeting.id).model_dump_json()
    store.attach_audio(meeting.id, second, expected_body=body, expected_audio=first)
    with pytest.raises(ValueError):
        store.attach_audio(meeting.id, third, expected_body=body, expected_audio=first)
    assert store.audio(meeting.id) == second.resolve()
    assert first.read_bytes() == b"original"


def test_simultaneous_audio_attach_has_one_winner(settings):
    store = Store(settings.database_path)
    meeting = Meeting(title="race")
    store.save(meeting)
    old = settings.database_path.parent / "old.wav"
    old.write_bytes(b"old")
    store.attach_audio(meeting.id, old)
    body = store.get(meeting.id).model_dump_json()
    barrier = threading.Barrier(2)

    def attach(index):
        candidate = settings.database_path.parent / f"candidate-{index}.wav"
        candidate.write_bytes(b"new")
        barrier.wait(timeout=5)
        try:
            store.attach_audio(meeting.id, candidate, expected_body=body, expected_audio=old)
            return candidate.resolve()
        except ValueError:
            candidate.unlink()
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attach, (1, 2)))
    assert len([path for path in outcomes if path is not None]) == 1
    assert store.audio(meeting.id) in outcomes
    assert old.read_bytes() == b"old"


def test_api_failed_attach_removes_staged_audio(settings, monkeypatch):
    app = create_app(settings, DummyProcessor)
    meeting = Meeting(title="race")
    app.state.store.save(meeting)
    with TestClient(app) as client:
        def reject(*args, **kwargs):
            raise ValueError("Audio changed")

        monkeypatch.setattr(app.state.store, "attach_audio", reject)
        result = client.post(f"/api/v1/meetings/{meeting.id}/audio",
                             files={"file": ("secret.wav", b"new")})
        assert result.status_code == 409
        assert not list((settings.data_root / meeting.id).iterdir())


class DummyProcessor:
    def __init__(self, settings, store):
        self.catalog = None

    async def run(self, attempt_id):
        await asyncio.sleep(0)


def test_second_app_cannot_recover_active_attempt(settings):
    first = create_app(settings, DummyProcessor)
    second = create_app(settings, DummyProcessor)
    with TestClient(first):
        store, meeting, _, attempt = admitted(settings)
        with pytest.raises(RuntimeError, match="already owns"), TestClient(second):
            pass
        assert store.get_attempt(attempt.id).status == "queued"
        assert store.get(meeting.id).active_attempt_id == attempt.id
    with TestClient(second):
        assert store.get_attempt(attempt.id).status == "failed"


def test_database_lock_across_processes(settings):
    lock = RuntimeLock(settings.database_path)
    probe = ("from meeting_protocol.runtime_lock import RuntimeLock; "
             "from pathlib import Path; import sys; "
             "lock=RuntimeLock(Path(sys.argv[1])); lock.acquire(); lock.release()")
    lock.acquire()
    try:
        failed = subprocess.run([sys.executable, "-c", probe, str(settings.database_path)],
                                capture_output=True, text=True, check=False)
        assert failed.returncode != 0 and "already owns" in failed.stderr
    finally:
        lock.release()
    assert subprocess.run([sys.executable, "-c", probe, str(settings.database_path)],
                          capture_output=True, check=False).returncode == 0


async def test_worker_preflight_failure_never_normalizes(settings, monkeypatch):
    import meeting_protocol.processor as module
    from meeting_protocol.profiles import ProfileError

    store, meeting, _, attempt = admitted(settings)
    normalized = []
    monkeypatch.setattr(module, "normalize_audio", lambda *args: normalized.append(args))

    async def fail(snapshot):
        raise ProfileError("server_unavailable", None, "sensitive/private/path")

    monkeypatch.setattr(module.ProfileCatalog, "from_settings", lambda settings: SimpleNamespace(preflight=fail))
    processor = Processor(settings, store)
    await asyncio.gather(processor.run(attempt.id), processor.run(attempt.id))
    assert normalized == []
    assert store.get_attempt(attempt.id).status == "failed"
    assert "private" not in (store.get(meeting.id).error or "")


async def test_duplicate_workers_normalize_only_once(settings, monkeypatch):
    import meeting_protocol.processor as module

    store, _, _, attempt = admitted(settings)

    async def preflight(snapshot):
        return None

    monkeypatch.setattr(module.ProfileCatalog, "from_settings",
                        lambda settings: SimpleNamespace(preflight=preflight))
    processor = Processor(settings, store)
    normalized = []

    def normalize(source, target, executable):
        normalized.append(source)
        target.write_bytes(b"normalized")

    class EmptyASR:
        def transcribe(self, path):
            return []

    monkeypatch.setattr(module, "normalize_audio", normalize)
    processor.asr = EmptyASR()
    await asyncio.gather(processor.run(attempt.id), processor.run(attempt.id))
    assert len(normalized) == 1
    assert store.get_attempt(attempt.id).status == "failed"
