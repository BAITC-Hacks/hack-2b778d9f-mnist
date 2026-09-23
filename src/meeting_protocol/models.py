from datetime import date
from enum import StrEnum
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


def uid() -> str:
    return str(uuid4())


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DeadlineType(StrEnum):
    exact = "exact"
    relative = "relative"
    event = "event"
    absent = "absent"


class TaskStatus(StrEnum):
    new = "new"
    in_progress = "in_progress"
    completed = "completed"


class ProcessingStatus(StrEnum):
    created = "created"
    uploaded = "uploaded"
    queued = "queued"
    normalizing = "normalizing"
    transcribing = "transcribing"
    diarizing = "diarizing"
    extracting = "extracting"
    completed = "completed"
    failed = "failed"


ProfileId = Annotated[str, StringConstraints(
    pattern=r"^[a-z0-9](?:[a-z0-9-]|\.[a-z0-9])*[a-z0-9]$", min_length=2, max_length=64, strict=True
)]


class ModelSelection(Model):
    asr: ProfileId | None = None
    diarization: ProfileId | None = None
    extract_tasks: ProfileId | None = None
    verify_tasks: ProfileId | None = None
    resolve_speakers: ProfileId | None = None
    generate_summary: ProfileId | None = None


class ResolvedBindings(Model):
    model_config = ConfigDict(extra="forbid", frozen=True)
    asr: ProfileId
    diarization: ProfileId
    extract_tasks: ProfileId
    verify_tasks: ProfileId
    resolve_speakers: ProfileId
    generate_summary: ProfileId


class Participant(Model):
    name: str = Field(min_length=1, max_length=200)


class TranscriptSegment(Model):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    speaker: str = "UNKNOWN"

    @model_validator(mode="after")
    def ordered(self):
        if self.end < self.start:
            raise ValueError("end must be >= start")
        return self


class SpeakerTurn(Model):
    speaker: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)


class SpeakerMapping(Model):
    speaker: str
    name: str | None = None
    confidence: float = Field(ge=0, le=1)
    manual: bool = False


class TaskCandidate(Model):
    action: str = Field(min_length=1)
    assignee: str | None = None
    assigner: str | None = None
    topic: str | None = None
    deadline_raw: str | None = None
    deadline_type: DeadlineType = DeadlineType.absent
    evidence_start: float = Field(ge=0)
    evidence_end: float = Field(ge=0)
    evidence_text: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    needs_review: bool = False


class MeetingTask(TaskCandidate):
    id: str = Field(default_factory=uid)
    meeting_id: str
    deadline_normalized: date | None = None
    status: TaskStatus = TaskStatus.new


class MeetingDecision(Model):
    text: str
    evidence_start: float = Field(ge=0)
    evidence_end: float = Field(ge=0)
    evidence_text: str


class Extraction(Model):
    tasks: list[TaskCandidate] = Field(default_factory=list)
    decisions: list[MeetingDecision] = Field(default_factory=list)


class MappingResult(Model):
    mappings: list[SpeakerMapping] = Field(default_factory=list)


class MeetingSummary(Model):
    text: str = ""


class Meeting(Model):
    id: str = Field(default_factory=uid)
    title: str = Field(min_length=1, max_length=300)
    meeting_date: date | None = None
    participants: list[Participant] = Field(default_factory=list, max_length=100)
    status: ProcessingStatus = ProcessingStatus.created
    error: str | None = None
    transcript: list[TranscriptSegment] = Field(default_factory=list)
    speaker_mappings: list[SpeakerMapping] = Field(default_factory=list)
    tasks: list[MeetingTask] = Field(default_factory=list)
    decisions: list[MeetingDecision] = Field(default_factory=list)
    summary: MeetingSummary = Field(default_factory=MeetingSummary)
    model_selection: ModelSelection = Field(default_factory=ModelSelection)
    active_attempt_id: str | None = None
    latest_attempt_id: str | None = None
    results_attempt_id: str | None = None
