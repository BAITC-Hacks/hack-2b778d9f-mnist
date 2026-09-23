import wave
from io import BytesIO
import sqlite3
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from app.main import create_app
from app.store import Store


def wav_bytes(frame_rate=16000, frame_count=None):
    sample = BytesIO()
    with wave.open(sample, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(frame_rate)
        wav.writeframes(b"\0\0" * (frame_count if frame_count is not None else frame_rate))
    return sample.getvalue()


def assert_no_meetings(db):
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM meetings").fetchone()[0] == 0


def test_meeting_edits_survive_store_restart_and_delete_audio(tmp_path):
    db, files = tmp_path / "db.sqlite", tmp_path / "files"
    client = TestClient(create_app(db, files))
    response = client.post(
        "/meetings",
        data={"meeting_date": "2026-09-23", "roster": '["Алия"]'},
        files={"audio": ("example.wav", wav_bytes(), "audio/wav")},
    )
    assert response.status_code == 201
    meeting_id = response.json()["id"]
    audio_path = next(files.iterdir())
    assert audio_path.name != "example.wav"
    assert client.get(f"/meetings/{meeting_id}/audio").content == wav_bytes()

    reopened = Store(db, files)
    assert reopened.get(meeting_id)["roster"] == ["Алия"]
    correction = {"summary": "Нужна проверка Алии"}
    assert client.patch(f"/meetings/{meeting_id}", json=correction).json()["summary"] == correction["summary"]
    assert Store(db, files).get(meeting_id)["summary"] == correction["summary"]

    assert client.delete(f"/meetings/{meeting_id}").status_code == 204
    assert not audio_path.exists()
    assert client.get(f"/meetings/{meeting_id}").status_code == 404


def test_rejects_invalid_date_empty_audio_and_traversal_filename(tmp_path):
    db, files = tmp_path / "db.sqlite", tmp_path / "files"
    client = TestClient(create_app(db, files))
    valid_fields = {"meeting_date": "2026-09-23", "roster": "[]"}

    invalid_date = client.post(
        "/meetings",
        data={**valid_fields, "meeting_date": "2026-02-30"},
        files={"audio": ("valid.wav", wav_bytes(), "audio/wav")},
    )
    empty_audio = client.post(
        "/meetings",
        data=valid_fields,
        files={"audio": ("empty.wav", b"", "audio/wav")},
    )
    traversal = client.post(
        "/meetings",
        data=valid_fields,
        files={"audio": ("../../outside.wav", wav_bytes(), "audio/wav")},
    )

    assert invalid_date.status_code == 422
    assert empty_audio.status_code == 422
    assert traversal.status_code == 201
    assert len(list(files.iterdir())) == 1
    assert not (tmp_path / "outside.wav").exists()


def test_patch_only_accepts_editable_fields_and_valid_roster(tmp_path):
    client = TestClient(create_app(tmp_path / "db.sqlite", tmp_path / "files"))
    created = client.post(
        "/meetings",
        data={"meeting_date": "2026-09-23", "roster": "[]"},
        files={"audio": ("recording.wav", wav_bytes(), "audio/wav")},
    )
    meeting_id = created.json()["id"]

    assert client.patch(f"/meetings/{meeting_id}", json={"status": "done"}).status_code == 422
    assert client.patch(f"/meetings/{meeting_id}", json={"roster": "not-json"}).status_code == 422
    assert client.patch(f"/meetings/{meeting_id}", json={"summary": ""}).status_code == 200


def test_upload_accepts_exactly_600_seconds_and_rejects_longer_wav(tmp_path):
    db, files = tmp_path / "db.sqlite", tmp_path / "files"
    client = TestClient(create_app(db, files))
    fields = {"meeting_date": "2026-09-23", "roster": "[]"}

    boundary = client.post(
        "/meetings", data=fields,
        files={"audio": ("boundary.wav", wav_bytes(frame_rate=1, frame_count=600), "audio/wav")},
    )
    assert boundary.status_code == 201
    boundary_id = boundary.json()["id"]
    assert client.delete(f"/meetings/{boundary_id}").status_code == 204

    too_long = client.post(
        "/meetings", data=fields,
        files={"audio": ("too-long.wav", wav_bytes(frame_rate=1, frame_count=601), "audio/wav")},
    )
    assert too_long.status_code == 422
    assert not list(files.iterdir())
    assert_no_meetings(db)


def test_upload_rejects_truncated_declared_wav_payload_without_persisting(tmp_path):
    db, files = tmp_path / "db.sqlite", tmp_path / "files"
    client = TestClient(create_app(db, files))
    truncated = wav_bytes()[:-2]

    response = client.post(
        "/meetings",
        data={"meeting_date": "2026-09-23", "roster": "[]"},
        files={"audio": ("truncated.wav", truncated, "audio/wav")},
    )

    assert response.status_code == 422
    assert not list(files.iterdir())
    assert_no_meetings(db)


def test_delete_keeps_meeting_when_audio_unlink_fails(tmp_path, monkeypatch):
    db, files = tmp_path / "db.sqlite", tmp_path / "files"
    store = Store(db, files)
    meeting_id = store.create("2026-09-23", [], b"audio", "recording.wav")
    audio_path = files / f"{meeting_id}.wav"
    original_unlink = Path.unlink

    def fail_audio_unlink(path, *args, **kwargs):
        if path == audio_path:
            raise PermissionError("simulated locked file")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_audio_unlink)
    with pytest.raises(PermissionError, match="simulated locked file"):
        store.delete(meeting_id)

    assert store.get(meeting_id)["id"] == meeting_id
    assert audio_path.exists()


def review_meeting(client, db, files):
    response = client.post(
        "/meetings", data={"meeting_date": "2026-09-23"},
        files={"audio": ("recording.wav", wav_bytes(), "audio/wav")},
    )
    meeting_id = response.json()["id"]
    Store(db, files).finish(meeting_id, {
        "transcript": [{"id": "t1", "start": 0.0, "end": 1.0, "text": "Исходный текст"}],
        "summary": "", "actions": [{
            "text": "Проверить", "assignee": None, "deadline_phrase": None, "due_date": None,
            "source_turn_ids": ["t1"], "confirmation_turn_ids": ["t1"],
            "needs_review": False, "confirmation_checked": True,
        }],
    })
    return meeting_id


def test_review_patch_requires_complete_action_fields(tmp_path):
    db, files = tmp_path / "db.sqlite", tmp_path / "files"
    client = TestClient(create_app(db, files))
    meeting_id = review_meeting(client, db, files)

    response = client.patch(f"/meetings/{meeting_id}", json={"actions": [{"text": "Проверить"}]})

    assert response.status_code == 422


def test_changed_action_and_cited_transcript_reset_confirmation(tmp_path):
    db, files = tmp_path / "db.sqlite", tmp_path / "files"
    client = TestClient(create_app(db, files))
    meeting_id = review_meeting(client, db, files)
    meeting = client.get(f"/meetings/{meeting_id}").json()
    action = meeting["actions"][0]
    action["text"] = "Изменено"

    changed_action = client.patch(f"/meetings/{meeting_id}", json={"actions": [action]}).json()["actions"][0]
    assert changed_action["needs_review"] is True
    assert changed_action["confirmation_checked"] is False

    # An unchanged save may keep a manual confirmation; cited text edits may not.
    changed_action.update(needs_review=False, confirmation_checked=True)
    assert client.patch(f"/meetings/{meeting_id}", json={"actions": [changed_action]}).json()["actions"][0]["confirmation_checked"] is True
    meeting = client.get(f"/meetings/{meeting_id}").json()
    meeting["transcript"][0]["text"] = "Исправленный текст"
    reset = client.patch(f"/meetings/{meeting_id}", json={
        "transcript": meeting["transcript"], "actions": meeting["actions"],
    }).json()["actions"][0]
    assert reset["needs_review"] is True
    assert reset["confirmation_checked"] is False


def test_delete_export_failure_preserves_audio_and_meeting(tmp_path, monkeypatch):
    db, files = tmp_path / "db.sqlite", tmp_path / "files"
    client = TestClient(create_app(db, files))
    meeting_id = review_meeting(client, db, files)
    audio_path = files / f"{meeting_id}.wav"
    exports = files / meeting_id
    exports.mkdir()
    original_rmtree = __import__("shutil").rmtree

    def fail_exports(path, *args, **kwargs):
        if path == exports:
            raise PermissionError("locked export")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("app.store.shutil.rmtree", fail_exports)
    with pytest.raises(PermissionError, match="locked export"):
        Store(db, files).delete(meeting_id)
    assert Store(db, files).get(meeting_id)["id"] == meeting_id
    assert audio_path.exists()


def test_delete_rejects_processing_meeting(tmp_path):
    db, files = tmp_path / "db.sqlite", tmp_path / "files"
    client = TestClient(create_app(db, files))
    meeting_id = review_meeting(client, db, files)
    Store(db, files).set_status(meeting_id, "processing")

    response = client.delete(f"/meetings/{meeting_id}")

    assert response.status_code == 409
    assert Store(db, files).get(meeting_id)["id"] == meeting_id
