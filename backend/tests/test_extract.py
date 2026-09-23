import pytest
from fastapi.testclient import TestClient

from app.extract import _deadline, extract_draft
from app.main import create_app


TURNS = [
    {"id": "t1", "text": "Предлагаю аудит, ответственным будет юридический отдел."},
    {"id": "t2", "text": "Фиксируем аудит до пятницы."},
    {"id": "t3", "text": "Можно также обновить сайт."},
]


def test_suggestions_stay_candidates_and_dates_are_grounded():
    def chat(_messages, _schema):
        return {"summary": "Обсудили аудит и сайт", "actions": [
            {"text": "Провести аудит", "assignee": "юридический отдел",
             "deadline_phrase": "до пятницы", "due_date": "2030-01-01",
             "source_turn_ids": ["t1"], "confirmation_turn_ids": ["t2"],
             "needs_review": False},
            {"text": "Обновить сайт", "assignee": None,
             "deadline_phrase": "когда-нибудь", "due_date": "2030-01-01",
             "source_turn_ids": ["t3"], "confirmation_turn_ids": [],
             "needs_review": False},
        ]}
    result = extract_draft(TURNS, "2026-09-23", [], chat)
    assert all(action["needs_review"] for action in result["actions"])
    assert result["actions"][0]["assignee"] == "юридический отдел"
    assert result["actions"][0]["due_date"] == "2026-09-25"
    assert result["actions"][1]["due_date"] is None
    assert result["actions"][1]["assignee"] is None


@pytest.mark.parametrize("response", ["bad json", {"summary": "", "actions": [{
    "text": "Аудит", "assignee": None, "deadline_phrase": None, "due_date": None,
    "source_turn_ids": ["missing"], "confirmation_turn_ids": [], "needs_review": True,
}]}])
def test_invalid_response_is_visible_error(response):
    with pytest.raises(ValueError):
        extract_draft(TURNS, "2026-09-23", [], lambda *_: response)


def test_roster_and_uncited_text_cannot_invent_owner_or_deadline():
    turns = [{"id": "t1", "text": "Предлагаю подготовить отчёт."},
             {"id": "t2", "text": "Алия завтра проводит другое совещание."}]
    response = {"summary": "Обсудили отчёт", "actions": [{
        "text": "Подготовить отчёт", "assignee": "Алия", "deadline_phrase": "завтра",
        "due_date": "2026-09-24", "source_turn_ids": ["t1"],
        "confirmation_turn_ids": [], "needs_review": False,
    }]}
    action = extract_draft(turns, "2026-09-23", ["Алия"], lambda *_: response)["actions"][0]
    assert action["assignee"] is None
    assert action["deadline_phrase"] is None
    assert action["due_date"] is None
    assert action["needs_review"] is True


def test_owner_and_deadline_can_be_grounded_in_confirmation():
    turns = [{"id": "t1", "text": "Есепті дайындауды ұсынамын."},
             {"id": "t2", "text": "Бекітемін. Жауапты — Алия, ертең."}]
    response = {"summary": "Есеп дайындалады", "actions": [{
        "text": "Есепті дайындау", "assignee": "Алия", "deadline_phrase": "ертең",
        "due_date": None, "source_turn_ids": ["t1"],
        "confirmation_turn_ids": ["t2"], "needs_review": True,
    }]}
    action = extract_draft(turns, "2026-09-23", [], lambda *_: response)["actions"][0]
    assert action["assignee"] == "Алия"
    assert action["deadline_phrase"] == "ертең"
    assert action["due_date"] == "2026-09-24"


@pytest.mark.parametrize("phrase,day,expected", [
    ("до пятницы", "2026-09-23", "2026-09-25"),
    ("до пятницы", "2026-09-25", None),
    ("до следующей пятницы", "2026-09-23", None),
    ("к 15 октября", "2026-09-23", None),
    ("15 октября 2026 года", "2026-09-23", "2026-10-15"),
    ("2026 жылғы 15 қазанға дейін", "2026-09-23", "2026-10-15"),
    ("жұмаға дейін", "2026-09-23", "2026-09-25"),
    ("жұмаға дейін", "2026-09-25", None),
    ("келесі жұма", "2026-09-23", None),
    ("ертең", "2026-09-23", "2026-09-24"),
    ("через две недели", "2026-09-23", "2026-10-07"),
    ("екі апта ішінде", "2026-09-23", "2026-10-07"),
    ("до конца недели", "2026-09-23", None),
    ("31.02.2026", "2026-09-23", None),
])
def test_deadlines_resolve_only_when_calendar_meaning_is_clear(phrase, day, expected):
    assert _deadline(phrase, day) == expected


def test_review_requires_checked_evidence_and_keeps_segment_times(tmp_path, permitted_wav):
    import time
    result = {"transcript": [{"id": "t1", "start": 0.0, "end": 1.0,
                              "text": "Фиксируем аудит", "speaker_id": "speaker_0",
                              "overlap": False, "speaker_uncertain": False}],
              "summary": "", "actions": []}
    with TestClient(create_app(tmp_path / "db.sqlite", tmp_path / "files",
                               processor=lambda *_: result)) as client:
        meeting_id = client.post("/meetings", data={"meeting_date": "2026-09-23"},
                                 files={"audio": ("demo.wav", permitted_wav, "audio/wav")}).json()["id"]
        client.post(f"/meetings/{meeting_id}/process")
        for _ in range(100):
            if client.get(f"/meetings/{meeting_id}").json()["status"] == "review":
                break
            time.sleep(0.01)
        action = {"text": "Провести аудит", "assignee": None,
                  "deadline_phrase": None, "due_date": None,
                  "source_turn_ids": ["t1"], "confirmation_turn_ids": ["t1"],
                  "needs_review": False}
        assert client.patch(f"/meetings/{meeting_id}", json={"actions": [action]}).status_code == 422
        action["confirmation_checked"] = True
        saved = client.patch(f"/meetings/{meeting_id}", json={"actions": [action],
                             "speaker_names": {"speaker_0": "Алия"}})
        assert saved.status_code == 200
        assert saved.json()["speaker_names"] == {"speaker_0": "Алия"}
        changed = [dict(result["transcript"][0], start=0.2)]
        assert client.patch(f"/meetings/{meeting_id}", json={"transcript": changed}).status_code == 422
