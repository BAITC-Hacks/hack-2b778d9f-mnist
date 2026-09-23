from copy import deepcopy

import pytest

from app.review import validate_review


@pytest.fixture
def meeting():
    return {
        "roster": ["Алия"], "speaker_names": {"s0": "Алия"}, "summary": "Аудит",
        "transcript": [{"id": "t1", "start": 0.0, "end": 1.0, "text": "Фиксируем аудит",
                        "speaker_id": "s0", "overlap": False, "speaker_uncertain": False}],
        "actions": [{"text": "Провести аудит", "assignee": "Алия", "due_date": "2026-09-25",
                     "deadline_phrase": "до пятницы", "source_turn_ids": ["t1"],
                     "confirmation_turn_ids": ["t1"], "needs_review": False,
                     "confirmation_checked": True}],
    }


def test_missing_review_flag_saves_as_candidate(meeting):
    action = validate_review(meeting, {"actions": [{"text": "Проверить договор",
                                                  "source_turn_ids": ["t1"]}]})["actions"][0]
    assert action["needs_review"] is True
    assert action["confirmation_checked"] is False
    assert action["source_turn_ids"] == ["t1"]


def test_even_candidates_require_a_source_when_saved(meeting):
    with pytest.raises(ValueError, match="source evidence"):
        validate_review(meeting, {"actions": [{"text": "Проверить договор"}]})


@pytest.mark.parametrize("existing", [True, False])
def test_new_or_changed_action_must_be_saved_before_confirmation(meeting, existing):
    if not existing:
        meeting["actions"] = []
    changed = {"text": "Новое содержание", "assignee": None, "deadline_phrase": None,
               "due_date": None, "source_turn_ids": ["t1"], "confirmation_turn_ids": ["t1"],
               "needs_review": False, "confirmation_checked": True}
    saved = validate_review(meeting, {"actions": [changed]})
    assert saved["actions"][0]["needs_review"] is True
    assert saved["actions"][0]["confirmation_checked"] is False
    revised = {**meeting, **saved}
    checked = {**saved["actions"][0], "needs_review": False, "confirmation_checked": True}
    assert validate_review(revised, {"actions": [checked]})["actions"][0]["needs_review"] is False


@pytest.mark.parametrize("change", [
    {"source_turn_ids": []}, {"source_turn_ids": ["missing"]},
    {"confirmation_turn_ids": []}, {"confirmation_turn_ids": [True]},
    {"confirmation_checked": "true"}, {"needs_review": 0},
    {"due_date": "2026-02-30"}, {"due_date": "20260925"},
    {"assignee": {"name": "Алия"}}, {"text": "   "},
    {"text": "invalid\x00text"}, {"unexpected": True},
])
def test_malformed_confirmed_actions_are_rejected(meeting, change):
    action = {**meeting["actions"][0], **change}
    with pytest.raises(ValueError):
        validate_review(meeting, {"actions": [action]})


@pytest.mark.parametrize("change", [
    {"text": ["Changed"]}, {"start": float("nan")}, {"start": True},
    {"start": 0.2}, {"id": "other"}, {"overlap": "false"},
])
def test_transcript_schema_and_original_timing_are_preserved(meeting, change):
    with pytest.raises(ValueError):
        validate_review(meeting, {"transcript": [{**meeting["transcript"][0], **change}]})


def test_changing_evidence_requires_a_new_confirmation_after_save(meeting):
    transcript = [{**meeting["transcript"][0], "text": "Аудит только предлагается"}]
    changes = validate_review(meeting, {"transcript": transcript, "actions": deepcopy(meeting["actions"])})
    assert changes["actions"][0]["needs_review"] is True
    assert changes["actions"][0]["confirmation_checked"] is False
    saved = {**meeting, **changes}
    checked = {**saved["actions"][0], "needs_review": False, "confirmation_checked": True}
    assert validate_review(saved, {"actions": [checked]})["actions"][0]["needs_review"] is False


@pytest.mark.parametrize("changes", [{"roster": ["Алия", "Ерлан"]},
                                     {"speaker_names": {"s0": "Ерлан"}}])
def test_participant_changes_reset_existing_confirmation(meeting, changes):
    assert validate_review(meeting, changes)["actions"][0]["needs_review"] is True


def test_unknown_speaker_name_mapping_is_rejected(meeting):
    with pytest.raises(ValueError, match="unknown speaker"):
        validate_review(meeting, {"speaker_names": {"invented": "Алия"}})


def test_manual_attribution_of_unknown_voice_requires_explicit_name(meeting):
    meeting["transcript"][0]["speaker_id"] = None
    meeting["speaker_names"] = {}
    revised = [{**meeting["transcript"][0], "speaker_id": "MANUAL_1"}]
    with pytest.raises(ValueError, match="explicit name"):
        validate_review(meeting, {"transcript": revised})
    saved = validate_review(meeting, {"transcript": revised, "speaker_names": {"MANUAL_1": "Алия"}})
    assert saved["speaker_names"] == {"MANUAL_1": "Алия"}
    assert saved["actions"][0]["needs_review"] is True


def test_summary_edit_does_not_reset_confirmation(meeting):
    assert validate_review(meeting, {"summary": "Исправлено"}) == {"summary": "Исправлено"}
