import asyncio
import logging

from .audio import FasterWhisperASR, PyannoteDiarizer, SetupError, merge_speakers, normalize_audio
from .extraction import extract_meeting
from .llm import LocalLLM
from .models import ProcessingStatus


class Processor:
    def __init__(self, settings, store, asr=None, diarizer=None, llm=None):
        self.settings, self.store = settings, store
        self.asr = asr or FasterWhisperASR(settings)
        self.diarizer = diarizer or PyannoteDiarizer(settings)
        self.llm = llm or LocalLLM(settings)
        self.lock = asyncio.Semaphore(settings.processing_concurrency)

    async def run(self, meeting_id: str):
        async with self.lock:
            meeting = self.store.get(meeting_id)

            def stage(status):
                meeting.status = status
                self.store.save(meeting)

            try:
                if (
                    isinstance(self.diarizer, PyannoteDiarizer)
                    and not self.settings.diarization_model
                ):
                    raise SetupError(
                        "Set DIARIZATION_MODEL to a local pyannote model before processing."
                    )
                source = self.store.audio(meeting_id)
                if source is None:
                    raise SetupError("Upload an audio file first.")
                target = self.settings.data_root / meeting_id / "normalized.wav"
                target.parent.mkdir(parents=True, exist_ok=True)
                stage(ProcessingStatus.normalizing)
                await asyncio.to_thread(normalize_audio, source, target, self.settings.ffmpeg_path)
                stage(ProcessingStatus.transcribing)
                segments = await asyncio.to_thread(self.asr.transcribe, target)
                if not segments:
                    raise SetupError("No speech was recognized. Check the audio and ASR model.")
                stage(ProcessingStatus.diarizing)
                turns = await asyncio.to_thread(self.diarizer.diarize, target)
                if not turns:
                    raise SetupError(
                        "Diarization returned no speaker turns; check the model and audio."
                    )
                meeting.transcript = merge_speakers(segments, turns)
                stage(ProcessingStatus.extracting)
                budget = max(
                    800,
                    min(
                        6500,
                        (
                            self.settings.llm_context_size
                            - self.settings.llm_max_output_tokens
                            - 1800
                        )
                        // 2,
                    ),
                )
                meeting = await extract_meeting(meeting, self.llm, budget)
                meeting.error = None
                stage(ProcessingStatus.completed)
            except Exception as exc:
                logging.getLogger(__name__).exception("Meeting processing failed")
                meeting.error = (
                    str(exc)
                    if isinstance(exc, SetupError)
                    else "Processing failed. Check the local server/model configuration and application terminal logs."
                )
                stage(ProcessingStatus.failed)
