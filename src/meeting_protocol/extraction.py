from .deadlines import normalize_deadline
from .models import Meeting, MeetingTask, SpeakerMapping, TranscriptSegment


def chunk_transcript(
    segments: list[TranscriptSegment], max_chars: int = 6500
) -> list[list[TranscriptSegment]]:
    chunks: list[list[TranscriptSegment]] = []
    current: list[TranscriptSegment] = []
    size = 0
    for segment in segments:
        length = len(segment.model_dump_json())
        if length > max_chars:
            raise ValueError("Transcript segment exceeds context budget; use shorter ASR segments")
        if current and size + length > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(segment)
        size += length
    if current:
        chunks.append(current)
    return chunks


def supported_evidence(item, segments: list[TranscriptSegment]) -> bool:
    selected = [
        s
        for s in segments
        if s.start >= item.evidence_start - 0.05 and s.end <= item.evidence_end + 0.05
    ]
    if not selected or item.evidence_end < item.evidence_start:
        return False
    if (
        abs(selected[0].start - item.evidence_start) > 0.05
        or abs(selected[-1].end - item.evidence_end) > 0.05
    ):
        return False
    normalize = lambda value: " ".join(value.split()).casefold()
    return normalize(item.evidence_text) in normalize(" ".join(s.text for s in selected))


async def extract_meeting(meeting: Meeting, llm, budget: int = 6500) -> Meeting:
    seen = set()
    decisions = set()
    summaries = []
    proposed: dict[str, list[SpeakerMapping]] = {}
    meeting.tasks, meeting.decisions = [], []
    for chunk in chunk_transcript(meeting.transcript, budget):
        payload = {"transcript": [s.model_dump() for s in chunk]}
        candidates = await llm.extract_tasks(payload)
        # Verify individually to keep candidate + evidence requests bounded.
        for candidate in candidates.tasks:
            if not supported_evidence(candidate, chunk):
                continue
            evidence = [
                s
                for s in chunk
                if s.start >= candidate.evidence_start - 0.05
                and s.end <= candidate.evidence_end + 0.05
            ]
            checked = await llm.verify_tasks(
                {
                    "transcript": [s.model_dump() for s in evidence],
                    "tasks": [candidate.model_dump(mode="json")],
                }
            )
            for task in checked.tasks[:1]:
                if not supported_evidence(task, evidence):
                    continue
                if (
                    task.deadline_raw
                    and task.deadline_raw.casefold() not in task.evidence_text.casefold()
                ):
                    task.deadline_raw, task.needs_review = None, True
                for field in ("assignee", "assigner"):
                    person = getattr(task, field)
                    if (
                        person
                        and person not in {s.speaker for s in evidence}
                        and person.casefold() not in task.evidence_text.casefold()
                    ):
                        setattr(task, field, None)
                        task.needs_review = True
                key = (task.action.casefold().strip(), task.assignee, round(task.evidence_start))
                if key in seen:
                    continue
                seen.add(key)
                normalized, kind, review = normalize_deadline(
                    task.deadline_raw, task.deadline_type, meeting.meeting_date
                )
                task.deadline_type = kind
                task.needs_review |= review or task.confidence < 0.75 or task.assignee is None
                meeting.tasks.append(
                    MeetingTask(
                        **task.model_dump(), meeting_id=meeting.id, deadline_normalized=normalized
                    )
                )
        for decision in candidates.decisions:
            if supported_evidence(decision, chunk):
                checked = await llm.verify_tasks({**payload, "decisions": [decision.model_dump()]})
                for verified in checked.decisions[:1]:
                    if supported_evidence(verified, chunk) and verified.text not in decisions:
                        decisions.add(verified.text)
                        meeting.decisions.append(verified)
        summaries.append((await llm.generate_summary(payload)).text)
        if meeting.participants:
            mappings = await llm.resolve_speakers(
                {**payload, "participants": [p.name for p in meeting.participants]}
            )
            for mapping in mappings.mappings:
                if mapping.speaker in {s.speaker for s in chunk} and mapping.name in {
                    p.name for p in meeting.participants
                }:
                    mapping.manual = False
                    proposed.setdefault(mapping.speaker, []).append(mapping)
    manual = {m.speaker: m for m in meeting.speaker_mappings if m.manual}
    for speaker in sorted({s.speaker for s in meeting.transcript}):
        options = proposed.get(speaker, [])
        strong = [m for m in options if m.confidence >= 0.85]
        if speaker not in manual:
            manual[speaker] = (
                max(strong, key=lambda m: m.confidence)
                if strong and len({m.name for m in strong}) == 1
                else SpeakerMapping(speaker=speaker, confidence=0)
            )
    meeting.speaker_mappings = list(manual.values())
    # Store confidently resolved people as stable speaker identities so a manual
    # name correction updates both transcript and tasks without another model run.
    name_to_speakers: dict[str, list[str]] = {}
    for mapping in meeting.speaker_mappings:
        if mapping.name and (mapping.manual or mapping.confidence >= 0.85):
            name_to_speakers.setdefault(mapping.name, []).append(mapping.speaker)
    for task in meeting.tasks:
        for field in ("assignee", "assigner"):
            identity = getattr(task, field)
            matches = name_to_speakers.get(identity, [])
            if len(matches) == 1:
                setattr(task, field, matches[0])
    # Hierarchical reduction, with fixed-size groups and a strictly shrinking count.
    while len(summaries) > 1:
        reduced = []
        for i in range(0, len(summaries), 2):
            parts = [text[: budget // 2] for text in summaries[i : i + 2]]
            reduced.append((await llm.generate_summary({"partial_summaries": parts})).text)
        summaries = reduced
    meeting.summary.text = summaries[0] if summaries else ""
    return meeting


def display_name(meeting: Meeting, identity: str | None) -> str | None:
    for mapping in meeting.speaker_mappings:
        if (
            mapping.speaker == identity
            and mapping.name
            and (mapping.manual or mapping.confidence >= 0.85)
        ):
            return mapping.name
    return identity
