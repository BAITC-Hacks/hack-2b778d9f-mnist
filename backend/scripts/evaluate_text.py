"""Evaluate real local extraction on synthetic text; semantic scoring stays manual.

Run from the repository root:
    PYTHONPATH=backend python3 backend/scripts/evaluate_text.py --output /tmp/text-report.json

This does not run ASR or diarization, and it cannot establish audio quality.
The overlap fixture supplies both scripted utterances; no audio is separated.
"""

import argparse
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import sys
import time


DEFAULT_FIXTURES = Path(__file__).resolve().parents[2] / "evaluation" / "simulated.json"


def deny_external_connections(event, arguments):
    """Guard this Python process; the separate Ollama process is not isolated."""
    if event != "socket.connect" or not isinstance(arguments[1], tuple):
        return
    try:
        if ipaddress.ip_address(arguments[1][0]).is_loopback:
            return
    except ValueError:
        pass
    raise RuntimeError("Text evaluation blocked an external network connection")


def machine_checks(actual: dict, turns: list[dict]) -> dict:
    """Check structural invariants only, without matching expected semantics."""
    ids = {turn["id"] for turn in turns}
    actions = actual["actions"]
    unknown_references = []
    missing_sources = []
    missing_review = []
    human_confirmation = []
    for index, action in enumerate(actions):
        if not action.get("source_turn_ids"):
            missing_sources.append(index)
        for field in ("source_turn_ids", "confirmation_turn_ids"):
            for reference in action.get(field, []):
                if reference not in ids:
                    unknown_references.append({
                        "action_index": index, "field": field, "turn_id": reference,
                    })
        if action.get("needs_review") is not True:
            missing_review.append(index)
        if action.get("confirmation_checked", False) is not False:
            human_confirmation.append(index)
    return {
        "action_count": len(actions),
        "evidence_ids_exist": not unknown_references,
        "unknown_references": unknown_references,
        "every_action_has_source": not missing_sources,
        "actions_missing_sources": missing_sources,
        "every_action_needs_review": not missing_review,
        "actions_without_review": missing_review,
        "no_automatic_human_confirmation": not human_confirmation,
        "actions_with_human_confirmation": human_confirmation,
        "note": "These checks do not detect omitted or semantically incorrect actions.",
    }


def evaluate_case(fixture: dict, extractor) -> dict:
    """Keep full expected and actual data together for a human reviewer."""
    started = time.monotonic()
    result = {
        "id": fixture.get("id"),
        "fixture": fixture,
        "input": None,
        "actual": None,
        "machine_checks": None,
        "semantic_review": "pending",
    }
    try:
        planned_turns = fixture["turns"]
        if not isinstance(planned_turns, list):
            raise ValueError("Fixture turns must be a list")
        if fixture.get("language") == "none" and not planned_turns:
            result.update(
                status="skipped",
                reason="Silence belongs to audio evaluation; extraction is not invoked.",
                semantic_review="not_applicable",
            )
            return result
        if not planned_turns:
            raise ValueError("A non-silence fixture must contain transcript turns")
        turns = []
        for turn in planned_turns:
            overlap = bool(turn.get("overlap", False))
            speaker = None if overlap else turn.get("expected_speaker_id")
            turns.append({
                "id": turn["id"], "start": turn["start"], "end": turn["end"],
                "text": turn["text"], "speaker_id": speaker, "overlap": overlap,
                "speaker_uncertain": speaker is None, "timestamp_uncertain": False,
            })
        if len({turn["id"] for turn in turns}) != len(turns):
            raise ValueError("Fixture transcript IDs must be unique")
        result["input"] = {
            "meeting_date": fixture["meeting_date"],
            "roster": fixture["participants"],
            "turns": turns,
        }
        actual = extractor(turns, fixture["meeting_date"], fixture["participants"])
        result["actual"] = actual
        checks = machine_checks(actual, turns)
        result["machine_checks"] = checks
        result["status"] = "completed" if all(checks[key] for key in (
            "evidence_ids_exist", "every_action_has_source", "every_action_needs_review",
            "no_automatic_human_confirmation",
        )) else "contract_failed"
    except Exception as error:
        result.update(status="failed", error={"type": type(error).__name__, "message": str(error)})
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--output", type=Path, required=True, help="Full local JSON report")
    args = parser.parse_args(argv)
    if args.fixtures.resolve() == args.output.resolve():
        parser.error("--output must differ from --fixtures")
    started = time.monotonic()
    report = {
        "schema_version": 1,
        "evaluation_kind": "extraction_from_synthetic_reference_text",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "fixtures_path": str(args.fixtures.resolve()),
        "status": "running",
        "semantic_review": "pending",
        "limitations": [
            "No ASR, audio diarization, word error rate or speaker accuracy measured.",
            "Scripted speaker IDs are inputs, not model predictions; overlaps are marked uncertain.",
            "Expected actions, proposals and review notes remain in each full fixture for human comparison.",
            "Completed means extraction and structural checks completed, not semantic correctness.",
        ],
        "network": {
            "python_connections": "loopback_only",
            "ollama_process_isolation": "not_checked",
        },
        "cases": [],
    }
    exit_code = 1
    try:
        data = json.loads(args.fixtures.read_text(encoding="utf-8"))
        fixtures = data["fixtures"]
        if not isinstance(fixtures, list) or not fixtures:
            raise ValueError("Expected a non-empty fixtures list")
        if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in fixtures):
            raise ValueError("Each fixture must be an object with a string ID")
        if len({item["id"] for item in fixtures}) != len(fixtures):
            raise ValueError("Fixture IDs must be unique")
        report["fixture_provenance"] = data.get("provenance")
        sys.addaudithook(deny_external_connections)
        from app.extract import extract_draft, ollama_chat

        def extract(turns, meeting_date, roster):
            return extract_draft(turns, meeting_date, roster, ollama_chat)

        write_report(args.output, report)
        for fixture in fixtures:
            report["current_fixture"] = fixture["id"]
            write_report(args.output, report)
            result = evaluate_case(fixture, extract)
            report["cases"].append(result)
            write_report(args.output, report)
            print(json.dumps({"id": result["id"], "status": result["status"],
                              "elapsed_seconds": result["elapsed_seconds"]}), flush=True)
        statuses = [case["status"] for case in report["cases"]]
        report["counts"] = {status: statuses.count(status) for status in (
            "completed", "failed", "contract_failed", "skipped",
        )}
        exit_code = 0 if "completed" in statuses and all(
            status in {"completed", "skipped"} for status in statuses
        ) else 1
        report["status"] = "completed" if exit_code == 0 else "failed"
        report.pop("current_fixture", None)
    except KeyboardInterrupt:
        report.update(status="interrupted", error="Interrupted before all fixtures completed")
        exit_code = 130
    except Exception as error:
        report.update(status="failed", error={"type": type(error).__name__, "message": str(error)})
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["exit_code"] = exit_code
        write_report(args.output, report)
    print(f"Full report: {args.output}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
