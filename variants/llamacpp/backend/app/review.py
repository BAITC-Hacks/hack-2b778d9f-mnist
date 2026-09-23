"""Validate secretary edits and keep human confirmation tied to current evidence."""

import math
import re
from datetime import date


EDITABLE = {"roster", "transcript", "summary", "actions", "speaker_names"}
TURN_FIELDS = {"id", "start", "end", "text", "speaker_id", "overlap",
               "speaker_uncertain", "timestamp_uncertain"}
ACTION_FIELDS = {"text", "assignee", "deadline_phrase", "due_date", "source_turn_ids",
                 "confirmation_turn_ids", "needs_review", "confirmation_checked"}


def _text(value, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{label} must be {'a' if empty else 'a non-empty'} string")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", value):
        raise ValueError(f"{label} contains invalid document characters")
    return value


def _boolean(value, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def _list(value, label: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _references(value, label: str, ids: set[str]) -> list[str]:
    references = [_text(item, label) for item in _list(value, label)]
    if len(set(references)) != len(references) or not set(references) <= ids:
        raise ValueError(f"{label} must contain unique existing transcript IDs")
    return references


def _transcript(value) -> list[dict]:
    result = []
    for turn in _list(value, "transcript"):
        if not isinstance(turn, dict) or set(turn) - TURN_FIELDS:
            raise ValueError("Invalid transcript fields")
        item = dict(turn)
        for key in ("id", "text"):
            item[key] = _text(item.get(key), f"Transcript {key}", empty=key == "text")
        for key in ("start", "end"):
            number = item.get(key)
            try:
                valid_time = type(number) in (int, float) and math.isfinite(number) and number >= 0
            except OverflowError:
                valid_time = False
            if not valid_time:
                raise ValueError("Transcript times must be finite non-negative numbers")
        if item["end"] < item["start"]:
            raise ValueError("Transcript end must not precede its start")
        if item.get("speaker_id") is not None:
            _text(item["speaker_id"], "speaker_id")
        for key in ("overlap", "speaker_uncertain", "timestamp_uncertain"):
            if key in item:
                _boolean(item[key], key)
        result.append(item)
    if len({turn["id"] for turn in result}) != len(result):
        raise ValueError("Transcript IDs must be unique")
    return result


def _actions(value, ids: set[str]) -> list[dict]:
    result = []
    for action in _list(value, "actions"):
        if not isinstance(action, dict) or set(action) - ACTION_FIELDS:
            raise ValueError("Invalid action fields")
        item = {"text": _text(action.get("text"), "Action text").strip()}
        for key in ("assignee", "deadline_phrase", "due_date"):
            value = action.get(key)
            item[key] = None if value is None else _text(value, key).strip()
        if item["due_date"] is not None:
            try:
                if date.fromisoformat(item["due_date"]).isoformat() != item["due_date"]:
                    raise ValueError
            except ValueError:
                raise ValueError("due_date must be an ISO calendar date YYYY-MM-DD") from None
        for key in ("source_turn_ids", "confirmation_turn_ids"):
            item[key] = _references(action.get(key, []), key, ids)
        if not item["source_turn_ids"]:
            raise ValueError("Every saved action requires source evidence")
        item["needs_review"] = _boolean(action.get("needs_review", True), "needs_review")
        item["confirmation_checked"] = _boolean(action.get("confirmation_checked", False),
                                                "confirmation_checked")
        if not item["needs_review"] and not (
            item["confirmation_checked"] and item["source_turn_ids"] and item["confirmation_turn_ids"]
        ):
            raise ValueError("Confirmed actions require checked source and confirmation evidence")
        result.append(item)
    return result


def validate_review(meeting: dict, changes: object) -> dict:
    """Return validated edits; changes to evidence reset every action's confirmation.

    Transcript IDs/times are immutable. Names are manually verified mappings to
    observed speaker IDs, independent of the roster. Evidence and participant
    changes require confirmation in a subsequent save, so an old checked box
    cannot silently confirm a revised transcript.
    """
    if not isinstance(changes, dict) or not changes or set(changes) - EDITABLE:
        raise ValueError("Expected non-empty editable meeting fields")
    result = dict(changes)
    merged = {**meeting, **changes}
    roster = [_text(person, "Participant name").strip()
              for person in _list(merged.get("roster", []), "roster")]
    if len(set(roster)) != len(roster):
        raise ValueError("Participant names must be unique")
    if "roster" in changes:
        result["roster"] = roster
    _text(merged.get("summary", ""), "summary", empty=True)
    transcript = _transcript(merged.get("transcript", []))
    original = _transcript(meeting.get("transcript", []))
    if [(t["id"], t["start"], t["end"]) for t in transcript] != [
        (t["id"], t["start"], t["end"]) for t in original
    ]:
        raise ValueError("Transcript IDs and times cannot be changed")
    if "transcript" in changes:
        result["transcript"] = transcript
    observed_speakers = {t["speaker_id"] for t in original if t.get("speaker_id")}
    speakers = {t["speaker_id"] for t in transcript if t.get("speaker_id")}
    speaker_names = merged.get("speaker_names", {})
    if not isinstance(speaker_names, dict):
        raise ValueError("speaker_names must map known speakers to names")
    names = {}
    for speaker, person in speaker_names.items():
        if _text(speaker, "Speaker ID") not in speakers:
            raise ValueError("speaker_names contains an unknown speaker")
        name = _text(person, "Speaker name", empty=True).strip()
        if name:
            names[speaker] = name
    new_speakers = speakers - observed_speakers
    if any(not re.fullmatch(r"MANUAL_[1-9][0-9]*", speaker) or speaker not in names
           for speaker in new_speakers):
        raise ValueError("New manually reviewed speakers require a MANUAL_n ID and an explicit name")
    if "speaker_names" in changes:
        result["speaker_names"] = names
    actions = _actions(merged.get("actions", []), {turn["id"] for turn in transcript})
    evidence_changed = (transcript != original or roster != meeting.get("roster", [])
                        or names != meeting.get("speaker_names", {}))
    previous_actions = _actions(meeting.get("actions", []), {turn["id"] for turn in original})
    content_fields = ACTION_FIELDS - {"needs_review", "confirmation_checked"}
    for index, action in enumerate(actions):
        changed_action = index >= len(previous_actions) or any(
            action[field] != previous_actions[index][field] for field in content_fields
        )
        if evidence_changed or changed_action:
            action["needs_review"] = True
            action["confirmation_checked"] = False
    if "actions" in changes or evidence_changed:
        result["actions"] = actions
    return result
