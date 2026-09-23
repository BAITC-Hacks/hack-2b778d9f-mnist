"""Create small local CPU-only MMS-TTS fixtures; model downloads are not performed.

Usage:
  MMS_TTS_KAZ_MODEL_PATH=/local/mms-tts-kaz \
  MMS_TTS_RUS_MODEL_PATH=/local/mms-tts-rus \
  python backend/scripts/make_synthetic_audio.py --output-dir /local/tts-fixtures

Model sources (Meta MMS, Vineel Pratap et al.; each CC-BY-NC-4.0):
  https://huggingface.co/facebook/mms-tts-kaz
  https://huggingface.co/facebook/mms-tts-rus
  https://creativecommons.org/licenses/by-nc/4.0/
API: https://huggingface.co/docs/transformers/model_doc/vits

Use these models for noncommercial diagnostic fixtures with attribution. A
hackathon label does not waive their NonCommercial restriction. This helper is
not a production TTS dependency. Listen to the audio before treating the input
script as a transcript reference. Concatenating two monolingual synthetic voices
does not establish natural code-switching quality or multi-person diarization.
"""

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import wave
from datetime import datetime, timezone
from pathlib import Path


SAMPLE_RATE = 16000
MODEL_LICENSE = "CC-BY-NC-4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-nc/4.0/"
CLIPS = {
    "kazakh": ("kaz", "Дана, ертең есепті дайындаңыз."),
    "mixed_kaz": ("kaz", "Марат, ертең."),
    "mixed_rus": ("rus", "Подготовьте список рисков."),
}


def local_model_directory(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"Set {name} to an already downloaded local model directory")
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"Local model directory does not exist: {path}")
    return path


def sha256(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    if args.threads < 1:
        parser.error("--threads must be positive")
    paths = {"kaz": local_model_directory("MMS_TTS_KAZ_MODEL_PATH"),
             "rus": local_model_directory("MMS_TTS_RUS_MODEL_PATH")}
    outputs = [args.output_dir / name for name in ("kazakh.wav", "mixed.wav", "manifest.json")]
    if any(path.exists() for path in outputs):
        parser.error("Output files already exist; choose a new --output-dir")

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    import numpy as np
    import torch
    from transformers import AutoTokenizer, VitsModel

    torch.set_num_threads(args.threads)
    clips, model_records = {}, []
    for language, path in paths.items():
        tokenizer = AutoTokenizer.from_pretrained(str(path), local_files_only=True)
        requires_uroman = bool(getattr(tokenizer, "is_uroman", False))
        if requires_uroman and importlib.util.find_spec("uroman") is None:
            raise RuntimeError(f"{language} tokenizer requires the local uroman package; install it before this offline run")
        model = VitsModel.from_pretrained(str(path), local_files_only=True).to("cpu").eval()
        if model.config.sampling_rate != SAMPLE_RATE:
            raise RuntimeError(f"Expected a 16 kHz checkpoint, got {model.config.sampling_rate}: {path}")
        for index, (clip_id, (clip_language, text)) in enumerate(CLIPS.items()):
            if clip_language != language:
                continue
            torch.manual_seed(args.seed + index)
            inputs = tokenizer(text, return_tensors="pt")
            with torch.inference_mode():
                samples = model(**inputs).waveform[0].float().cpu().numpy()
            if (not 0 < samples.size <= SAMPLE_RATE * 60 or not np.isfinite(samples).all()
                    or np.max(np.abs(samples)) < 1e-5):
                raise RuntimeError(f"Empty, invalid or unexpectedly long synthesized clip: {clip_id}")
            clips[clip_id] = samples
        model_records.append({
            "id": f"facebook/mms-tts-{language}", "directory": str(path),
            "source": f"https://huggingface.co/facebook/mms-tts-{language}",
            "creator": "Meta MMS / Vineel Pratap et al.",
            "model_license": MODEL_LICENSE, "model_license_url": LICENSE_URL,
            "tokenizer_is_uroman": requires_uroman,
            "artifact_sha256": {name: sha256(path / name) for name in
                                ("config.json", "tokenizer_config.json", "model.safetensors", "pytorch_model.bin")
                                if (path / name).is_file()},
        })
        del model, tokenizer

    args.output_dir.mkdir(parents=True, exist_ok=True)
    fixtures = []
    for fixture_id, clip_ids, expected_action in (
        ("kazakh", ["kazakh"], {"text": "Есепті дайындау", "assignee": "Дана",
                                 "deadline_phrase": "ертең", "due_date": "2026-09-24"}),
        ("mixed", ["mixed_kaz", "mixed_rus"], {"text": "Подготовить список рисков", "assignee": "Марат",
                                                "deadline_phrase": "ертең", "due_date": "2026-09-24"}),
    ):
        padding = np.zeros(round(SAMPLE_RATE * 0.3), dtype=np.float32)
        chunks, segments = [padding], []
        offset = len(padding)
        for clip_id in clip_ids:
            samples = clips[clip_id]
            language, text = CLIPS[clip_id]
            segments.append({"start": offset / SAMPLE_RATE, "end": (offset + len(samples)) / SAMPLE_RATE,
                             "language": language, "intended_text": text,
                             "synthetic_voice_source": f"facebook/mms-tts-{language}"})
            chunks.extend((samples, padding))
            offset += len(samples) + len(padding)
        audio = np.concatenate(chunks)
        destination = args.output_dir / f"{fixture_id}.wav"
        with wave.open(str(destination), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        fixtures.append({"id": f"mms-tts-{fixture_id}", "audio_path": destination.name,
                         "sha256": sha256(destination), "duration_seconds": len(audio) / SAMPLE_RATE,
                         "meeting_date": "2026-09-23", "segments": segments,
                         "intended_text": " ".join(segment["intended_text"] for segment in segments),
                         "expected_actions_from_script": [expected_action],
                         "listening_verification_status": "pending", "asr_evaluation_status": "pending"})
    manifest = {
        "schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
        "generator": "make_synthetic_audio.py", "device": "cpu", "seed": args.seed,
        "sample_rate": SAMPLE_RATE, "channels": 1, "encoding": "PCM signed 16-bit little-endian",
        "versions": {name: importlib.metadata.version(name) for name in ("torch", "transformers", "numpy")},
        "models": model_records, "fixtures": fixtures,
        "limitations": ["Synthetic TTS audio; input text must be checked by listening before scoring ASR.",
                        "Mixed audio concatenates separate Kazakh and Russian models; language and voice change together.",
                        "No natural meeting, enrolled identity, overlap or multi-speaker diarization validation.",
                        "Model weights are CC-BY-NC-4.0; use is limited to noncommercial diagnostics with attribution.",
                        "A fixed seed does not guarantee identical audio across different library versions or hardware."],
    }
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"manifest": str(manifest_path), "audio": [str(path) for path in outputs[:2]]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
