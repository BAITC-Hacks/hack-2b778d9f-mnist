import asyncio
import logging
import shutil
from pathlib import Path

from .audio import FasterWhisperASR, PyannoteDiarizer, SetupError, merge_speakers, normalize_audio
from .extraction import extract_meeting
from .llm import LocalLLM
from .models import ProcessingStatus
from .profiles import ProfileCatalog, ProfileError


def check_audio_setup(source: Path, ffmpeg_path: str) -> None:
    """Cheap pre-admission/worker check; never expose the private source path."""
    if not source.is_file():
        raise SetupError("Uploaded audio is unavailable. Upload the audio again.")
    if shutil.which(ffmpeg_path) is None:
        raise SetupError("ffmpeg is unavailable. Install ffmpeg and set FFMPEG_PATH to its executable.")


class Processor:
    def __init__(self, settings, store, asr=None, diarizer=None, llm=None):
        self.settings, self.store = settings, store
        self.asr = asr or FasterWhisperASR(settings)
        self.diarizer = diarizer or PyannoteDiarizer(settings)
        self.llm = llm or LocalLLM(settings)
        self.lock = asyncio.Semaphore(settings.processing_concurrency)
        self.catalog = ProfileCatalog.from_settings(settings)

    async def run(self, attempt_id: str):
        await self._run_attempt(attempt_id)

    async def _run_attempt(self, attempt_id: str):
        async with self.lock:
            attempt = self.store.claim_attempt(attempt_id)
            if attempt is None:
                return
            meeting = self.store.get(attempt.meeting_id)
            fresh = type(meeting)(id=meeting.id, title=meeting.title, meeting_date=meeting.meeting_date,
                                  participants=meeting.participants, model_selection=meeting.model_selection)
            try:
                check_audio_setup(attempt.source_audio, self.settings.ffmpeg_path)
                await self.catalog.preflight(attempt.snapshot)
                asr_profile = attempt.snapshot.profile_for("asr")
                diar_profile = attempt.snapshot.profile_for("diarization")
                asr = self.asr if self.asr is not None and not isinstance(self.asr, FasterWhisperASR) else FasterWhisperASR(asr_profile)
                diarizer = self.diarizer if self.diarizer is not None and not isinstance(self.diarizer, PyannoteDiarizer) else PyannoteDiarizer(diar_profile)
                from .llm import LocalLLM
                llm = self.llm if self.llm is not None and not isinstance(self.llm, LocalLLM) else LocalLLM(self.settings, snapshot=attempt.snapshot)
                target = self.settings.data_root / meeting.id / f"normalized-{attempt.id}.wav"
                target.parent.mkdir(parents=True, exist_ok=True)
                def stage(status):
                    if not self.store.stage_attempt(attempt.id, status):
                        raise RuntimeError("Attempt is no longer active")
                await asyncio.to_thread(normalize_audio, attempt.source_audio, target, self.settings.ffmpeg_path)
                stage(ProcessingStatus.transcribing)
                segments = await asyncio.to_thread(asr.transcribe, target)
                if not segments:
                    raise SetupError("No speech was recognized. Check the audio and ASR model.")
                stage(ProcessingStatus.diarizing)
                turns = await asyncio.to_thread(diarizer.diarize, target)
                if not turns:
                    raise SetupError("Diarization returned no speaker turns; check the model and audio.")
                fresh.transcript = merge_speakers(segments, turns)
                stage(ProcessingStatus.extracting)
                llm_profiles = [attempt.snapshot.profile_for(op) for op in ("extract_tasks", "verify_tasks", "resolve_speakers", "generate_summary")]
                budget = max(1, min(6500, (min(p.context_size - p.max_output_tokens for p in llm_profiles) - 1800) // 2))
                fresh = await extract_meeting(fresh, llm, budget)
                self.store.finish_attempt(attempt.id, fresh)
            except Exception as exc:
                logging.getLogger(__name__).exception("Meeting attempt failed")
                safe_error = (str(exc) if isinstance(exc, SetupError) else
                              "Selected model setup is unavailable" if isinstance(exc, ProfileError) else
                              "Processing failed. Check the local server/model configuration and application terminal logs.")
                self.store.finish_attempt(attempt.id, fresh, safe_error)
