from pathlib import Path
import math
import re
from typing import Callable


def process_recording(
    source: Path,
    date: str,
    roster: list[str],
    asr: Callable,
    diarizer: Callable,
    extract: Callable,
) -> dict:
    turns = [turn for turn in asr(source) if str(turn.get("text", "")).strip()]
    if not turns:
        raise ValueError("No speech was recognized; review the recording and retry")
    for turn in turns:
        words = re.findall(r"\w+", turn["text"].casefold())
        # Whisper can loop on noise. A successful model call must not turn a
        # repeated hallucination into a reviewable protocol without a visible error.
        for width in range(1, 6):
            if any(words[index:index + width] * 6 == words[index:index + width * 6]
                   for index in range(len(words) - width * 6 + 1)):
                raise ValueError("ASR produced repetitive text; inspect the audio or use another local ASR model")
    speaker_intervals = diarizer(source)
    transcript = []
    for turn in turns:
        start, end = float(turn["start"]), float(turn["end"])
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError("ASR returned invalid segment timestamps")
        intersecting = [
            (max(start, speaker_start), min(end, speaker_end), speaker)
            for speaker_start, speaker_end, speaker in speaker_intervals
            if speaker_start < end and speaker_end > start
        ]
        speakers = {speaker for _, _, speaker in intersecting}
        overlaps = any(
            first[0] < second[1] and second[0] < first[1]
            for index, first in enumerate(intersecting)
            for second in intersecting[index + 1:]
            if first[2] != second[2]
        )
        # One voice may have multiple intervals separated by pauses. Merge their
        # coverage, while never resolving a true change of voice by majority vote.
        covered_seconds = 0.0
        previous_end = start
        for left, right, _ in sorted(intersecting):
            covered_seconds += max(0.0, right - max(left, previous_end))
            previous_end = max(previous_end, right)
        certain = (len(speakers) == 1 and covered_seconds / (end - start) >= 0.6
                   and not overlaps and not turn.get("timestamp_uncertain", False))
        overlap = overlaps
        transcript.append({
            "id": turn.get("id", f"t{len(transcript) + 1}"),
            "start": start,
            "end": end,
            "text": str(turn["text"]),
            "speaker_id": next(iter(speakers)) if certain else None,
            "overlap": overlap,
            "speaker_uncertain": not certain,
            "timestamp_uncertain": bool(turn.get("timestamp_uncertain", False)),
        })
    extracted = extract(transcript, date, roster)
    return {"transcript": transcript, "summary": extracted["summary"], "actions": extracted["actions"]}
