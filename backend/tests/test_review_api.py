import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store import Store


@pytest.mark.parametrize("changes", [
    {"roster": ["Алия", "Дана"]},
    {"speaker_names": {"s1": "Дана"}},
])
def test_api_cannot_restore_confirmation_after_participant_correction(tmp_path, changes):
    store = Store(tmp_path / "db", tmp_path / "files")
    meeting_id = store.create("2026-09-23", ["Алия"], b"audio", "")
    store.finish(meeting_id, {
        "transcript": [{"id": "t1", "start": 0, "end": 1, "text": "Поручаю аудит",
                        "speaker_id": "s1", "speaker_uncertain": False, "overlap": False}],
        "summary": "", "actions": [{"text": "Аудит", "assignee": "Алия",
            "deadline_phrase": None, "due_date": None, "source_turn_ids": ["t1"],
            "confirmation_turn_ids": ["t1"], "needs_review": False, "confirmation_checked": True}],
    })
    with TestClient(create_app(tmp_path / "db", tmp_path / "files")) as client:
        response = client.patch(f"/meetings/{meeting_id}", json=changes)
        assert response.status_code == 200
        action = response.json()["actions"][0]
        assert action["needs_review"] is True
        assert action["confirmation_checked"] is False
