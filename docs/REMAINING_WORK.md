# Remaining work

As of 2026-09-23. Historical offline runs had 33 then 64 passes; the latest full suite had **117 passed**, one Starlette/httpx deprecation warning. Prescribed Ruff, source-only mypy (14 files), JS syntax, four Node behavioral tests and unchanged-originals/whitespace checks passed. An optional `mypy .` over tests reported missing YAML stubs. These are not real inference, rendered/browser visual verification, or a successful real-model demo.

## P0 — Establish a real local run

- [x] Implement [task-specific model selection](MODEL_SELECTION.md) with configured defaults: pyannote speaker-diarization-3.1, OpenAI Whisper large-v3-turbo with a separate local CT2 runtime-artifact setting, and the existing Qwen GGUF for every LLM operation. Offline/fake-tested only; actual artifact compatibility still needs validation. Earlier community-1 setup recommendations are superseded.
- [x] Record bounded agent roles/file ownership in [OPENCODE_PROMPT.md](OPENCODE_PROMPT.md); keep QA/integration review and real-model experiments separate.

- [x] Run the available `.venv-meeting` offline checks: 117 pytest tests (one deprecation warning), Ruff, `python -m mypy` on 14 source files, JS syntax, four Node behavioral tests and unchanged originals passed. Rerun after future changes.
- [ ] Inspect the current environment safely. Do not overwrite `.env`; document needed additions or use temporary environment overrides.
- [ ] Install/locate a recent compatible llama.cpp Windows build. Run its actual `--help`; test `scripts/start_llama_server.ps1` against it.
- [ ] Load the existing Qwen GGUF in place and test a small extraction, verification, mapping and summary request. Confirm thinking behavior, JSON format support, truncation handling and fallback behavior.
- [ ] Install `.[models]` and establish compatible versions of faster-whisper, CTranslate2, PyTorch, pyannote (`pyannote.audio>=3.4,<4` is only a declared uninstalled range) and soundfile on Python 3.12/Windows. Do not claim 3.x compatibility without a real load.
- [ ] Prepare a compatible local CT2 conversion of `openai/whisper-large-v3-turbo` and complete offline `pyannote/speaker-diarization-3.1` pipeline/dependent weights. Model license acceptance and account credentials must be handled appropriately; do not bundle tokens.
- [ ] Confirm processing works without external network access after model preparation. Offline environment flags alone are not a traffic audit.
- [ ] Run short real audio end to end with CPU ASR/diarization and conservative llama GPU offload. Record settings, timings and memory use.

Done when a real audio meeting reaches completed with inspectable speaker transcript, grounded tasks, summary, saved results and downloadable exports, without remote inference.

## P1 — Correctness before the demo

Review [DESIGN_REVIEW.md](DESIGN_REVIEW.md) before implementing changes.

- [ ] Verify the new attempt-specific normalized-audio lookup after failed-upload replacement/retry with a focused regression test; the prior stale `normalized.wav` behavior was identified by code review, not exercised with a real recording.
- [ ] Preserve or explicitly version user task status and mapping edits during reprocessing. New diarization produces fresh results; manual speaker mappings do **not** automatically carry over. Review how to present/restore user edits safely, without reusing labels for different speakers.
- [ ] Check prompt/schema/repair request sizes against the installed model's context. Test long Russian/Kazakh input and many candidates.
- [ ] Add context-preserving chunk overlap or another bounded approach for assignments spanning turns/chunks.
- [ ] Separate supporting speaker presence from evidence of assignment; test instruction giver versus assignee, pronouns, direct address, multiple tasks, later deadline statements and negations.
- [ ] Make dropped candidates/corrections observable for review without displaying raw model output as trusted facts.
- [ ] Test summary faithfulness and avoid losing key information through arbitrary partial-summary truncation.
- [ ] Run `audio1` and `audio2`; use only a known actual meeting date, otherwise null. Store output in `data/`.
- [ ] Compare predictions with the untouched reference protocols independently. Record missed/invented tasks, assignee errors, deadline errors and evidence accuracy, including genuine ambiguities in references.

## P2 — Browser and document verification

- [ ] Open the real UI in an available browser; test create, upload, start, status polling and failure/retry paths.
- [ ] Test speaker correction after automatic inference and manual task status updates, including after refresh/restart.
- [ ] Verify every task's Source button seeks to the expected spoken evidence.
- [ ] Test switching meetings during fetch/polling and after failed/replaced uploads; ensure the selected meeting and its audio stay consistent.
- [ ] Test API-key entry, session persistence, wrong-key errors, loopback and LAN access. Never include keys in URLs or localStorage.
- [ ] Open/render DOCX and PDF with a long transcript, long task text, missing values, review flags and Kazakh characters. Inspect every page for clipping, wrapping and missing glyphs.
- [ ] Verify font fallback/error behavior and very long PDF table cells on Windows.

## P3 — Operational cleanup and reproducibility

- [ ] Enforce upload limits before or during multipart parsing, not only while copying a parsed UploadFile; test absent/misleading Content-Length and excess multipart fields.
- [ ] Test upload/process races, interrupted jobs and failed normalization; keep cleanup strictly inside generated runtime directories.
- [ ] Decide a bounded shutdown/cancellation policy and persist failures safely. Keep one worker; do not add a distributed queue.
- [ ] Verify preflight against real artifacts and llama-server: shared complete CT2/YAML weight checks, metadata-only dependency/version checks and `/models` plus read-only root/API-prefix GET `/props` `model_path` comparison are implemented and fake-tested. The server-reported absolute path is not a content/hash proof or a successful inference test. Keep secrets and paths out of HTTP errors; add a safe operator setup command if useful. GET `/props` needs no `--props` flag (only POST mutation does).
- [ ] Review wheel installation, prompt/static asset packaging, execution outside the repository root and runtime-path consistency.
- [ ] Pin a reproducible tested heavy-model stack after a successful run. The current lightweight snapshot is not sufficient.
- [ ] Fix or document the Starlette/httpx test-client deprecation warning after checking compatible versions.
- [ ] Update README and these handoff files with exact successful settings and measured limitations.

## Later, not required to prove the core demo

Teams/Zoom/Meet ingestion, reminders, SED integration, richer analytics, scalable audio streaming, a better review editor and broader multilingual calendar rules. Do not prioritize these ahead of a reliable, accurate local run.

## Evidence needed before saying the demo is ready

1. All offline checks pass with recorded versions.
2. At least one complete real-model run and both supplied recordings have been attempted, with failures and accuracy reported honestly.
3. Browser actions and exported document layouts have been visually checked.
4. No meeting data leaves the local processing boundary, no original data changes, and no secrets appear in API output.
5. Working launch commands/settings and remaining limitations are written down. Real-model verification is explicitly distinguished from injected-fake tests.
