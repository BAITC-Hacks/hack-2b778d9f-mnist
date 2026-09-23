# Decisions and implementation risks to revisit

Review date: 2026-09-23. These are findings from the code and recorded test coverage, not a claim that every concern has been reproduced at runtime. Do not replace the whole application to address them; prefer targeted fixes backed by regression cases.

## 1. Context budgeting and chunk boundaries — high priority

Current files: `extraction.py`, `processor.py`, `llm.py`.

Chunks are bounded by serialized character count. The processor estimates a budget from configured context and output sizes, but the request also includes prompts, JSON schema, participant names, candidate data and potentially an invalid answer plus repair instruction. This is not a tokenizer-aware context guarantee. There is no overlap, so an assignee in one chunk and action/deadline in the next may be separated. An oversized segment causes failure.

Reconsider: reserve space for the entire request and repair path; use measured local token counts or a defensible conservative bound. Add small segment overlap with deduplication, or carry bounded context with explicit evidence identities. Test Cyrillic/Kazakh, large participant lists, dense tasks and output truncation. Avoid adding another model service just to count tokens.

## 2. Evidence matching versus semantic grounding — high priority

Current file: `extraction.py`.

Exact quotes and timestamp checks establish that words occurred. They do not establish that the action, assignee or deadline interpretation is correct. The verifier receives only selected evidence for each task, which may omit the context resolving pronouns or later corrections. A speaker ID is accepted by the deterministic person check if that speaker occurs in evidence; presence alone does not establish responsibility. Conversely, exact name substrings may reject inflected names or supported references outside the quotation. The same local model extracts and verifies, so their errors may be correlated.

Reconsider: give the verifier bounded surrounding context while retaining narrow evidence references. Separate speaker, addressed person, explicit assignee and inferred identity. Require affirmative support for each field; keep ambiguity visible. Build regression examples for the chair assigning work to someone else, negated/cancelled work, shared responsibility and several assignments per utterance.

## 3. Silent loss and candidate identity — high priority

Current file: `extraction.py`.

Candidates that fail evidence checks disappear. Only the first verified task/decision is retained per verification call. Deduplication uses action text, assignee and rounded start time, so paraphrases may survive and distinct repeated statements may collide. Verification output is not linked through a stable candidate ID. A completed meeting with few/no tasks may reflect valid silence or lost candidates, and the UI does not explain the difference.

Reconsider: define candidate identity and verifier outcomes such as accepted, corrected and rejected, with short review reasons. Keep unsupported predictions out of final tasks while recording useful local diagnostics. Constrain a verifier to the intended candidate and handle splitting explicitly. Evaluate precision and recall before choosing more aggressive deduplication.

## 4. Manual edits and identity over time — high priority

Current files: `extraction.py`, `api.py`, `models.py`.

Confident uniquely mapped participant names are converted to speaker IDs, enabling manual correction in displayed tasks. That improves the simple case but ties task identity to the diarization run. Reprocessing replaces tasks and their IDs/statuses. Manual mappings are retained by speaker label, although the same label can refer to a different person after a model/configuration change. Low-confidence name guesses are discarded rather than retained as reviewable suggestions.

Reconsider: keep source speaker identity, participant identity, inference provenance and manual override distinct. Preserve user edits against a processing revision, or explicitly create a new result version. Do not automatically transfer mappings between incompatible diarization runs. Decide how users should correct an assignee who never spoke and therefore has no speaker mapping. The current UI only edits task status, not task text/assignee/deadline.

## 5. Audio replacement and upload lifecycle — high priority

Current file: `api.py`.

The audio endpoint prefers an existing `normalized.wav` whenever the meeting is not actively processing. Failed meetings can accept new uploads, but upload does not invalidate the old normalized file. By code inspection, replacement after a failed run can therefore serve stale audio before reprocessing. A second editability check after writing an upload can raise a conflict outside the file-cleanup block, leaving an orphan. Audio-path and meeting-status writes are separate transactions. Upload limits are checked after the framework has already parsed/spooled multipart data.

Reconsider: identify audio revisions and use only normalized output belonging to the active source. Make generated-file cleanup and metadata transitions coherent, including rejected races. Enforce bounded incoming request bodies at the appropriate layer. Test replacement, two uploads, upload during processing, partial normalization and missing files. Never clean original data while cleaning runtime files.

## 6. Summary reliability — high priority

Current file: `extraction.py`.

Chunk summaries are generated from transcript content, not primarily verified task/decision records. Hierarchical reduction slices partial summaries to a character budget, potentially cutting a key statement or negation. Summaries have no evidence anchors or verifier pass, so task verification does not imply summary verification.

Reconsider: use bounded structured facts with references and verified tasks/decisions as summary inputs, preserve complete statements, and provide a clear manual review path. Test that summaries do not invent consensus or turn proposals into decisions. Keep summary generation bounded and simple.

## 7. Deadline policy and calendar ambiguity — medium priority

Current file: `deadlines.py`.

The initial resolver uses Russian phrases, same-year interpretation for yearless dates, same-day-inclusive weekdays and Sunday for the end of a week. These are policies, not universal interpretations. Some unsupported expressions retain a model-supplied type even if ambiguous. Kazakh relative phrases and business calendars are not implemented. The weekday matcher uses prefix matching rather than a complete grammar.

Reconsider: distinguish exact date, interval, event dependency and unresolved phrase. Display the policy/assumption where it affects a user's decision. Prefer review flags to speculative date arithmetic, especially around year boundaries and ambiguous same-day deadlines. Add invalid-date, trailing-text and Kazakh cases without involving the LLM in deterministic normalization.

## 8. Real speech dependencies and resource use — integration required

Current file: `audio.py`.

The adapters use local/cache loading and CPU defaults, and release model references between stages. None of this was measured with the real model stack. pyannote compatibility, downloaded weight completeness, Windows dependencies, mixed-language ASR quality and actual GPU memory release remain unverified. Diarization alignment assigns maximum-overlap speakers; overlapping speech and zero-length word times need evaluation. Errors outside a few expected loading exceptions become a generic processing failure.

Reconsider only after measurement: pin compatible versions, choose model/device settings based on actual RAM/VRAM/timing, improve actionable setup errors without leaking private paths, and add difficult alignment fixtures. Do not promise GPU coexistence on 6 GB or infer recognition quality from synthetic tests.

## 9. API and job lifecycle — medium priority

Current files: `api.py`, `processor.py`, `store.py`.

One worker and a semaphore fit the demo. They are not cross-process locking. Shutdown waits for jobs, thread-based inference has no explicit cancellation, and some operations can run a long time. Full meeting records are loaded for task lookup and rewritten on updates. Current tests do not establish behavior under multiple processes or simultaneous updates. HTTP on a trusted LAN does not encrypt API keys in transit; loopback access without a key also merits Host/Origin handling review.

Reconsider: keep the one-worker contract explicit; add bounded shutdown and coherent per-meeting mutation handling only as needed. Do not introduce Celery or PostgreSQL to solve a local demo's state transitions. Test Host/Origin/auth behavior without weakening loopback-only llama access. Keep sensitive diagnostics in local logs rather than public errors.

## 10. Browser audio and asynchronous UI — medium priority

Current file: `web/app.js`.

Authenticated audio is fetched into a full browser blob. That avoids putting keys in media URLs, but decoded WAVs can be large. The detail view is rebuilt during polling; requests are not cancelled when selection changes, and audio position/focus/playback can be affected. Browser correctness has not been visually tested.

Reconsider: first test rapid selection changes and polling races, then use request identity/cancellation and incremental updates where needed. Retain header-based authentication. Avoid replacing the simple UI framework-free approach unless there is a demonstrated need.

## 11. Validation strength, packaging and exports — medium priority

Current files: `models.py`, `llm.py`, `exports.py`, `config.py`, `pyproject.toml`.

Pydantic forbids extra fields and non-finite numbers but is not globally strict. End-before-start validation exists on transcript segments but not all evidence/turn models. Malformed JSON has bounded repair; malformed HTTP response envelopes, truncated generations and huge valid strings have less targeted handling. Prompt packaging has a wheel fallback, but installed-wheel behavior and root-relative configuration/data paths have not been tested. Export text/Unicode checks do not establish visual quality or safe handling of arbitrary control characters in ASR output.

Reconsider: enforce important domain invariants explicitly, add practical field/output bounds, distinguish JSON syntax failures from transport/envelope failures, and test an installed wheel outside the source root. Render exports with long rows and Kazakh glyphs before declaring them presentation-ready. Prefer small checks over broad abstractions.

## What should remain simple

Keep local processing, separate llama-server, immutable originals, configurable CPU/GPU use, SQLite, bounded model calls and plain dashboard assets. The highest-value next step is a real integration run with measured errors, followed by targeted corrections. Do not turn these review notes into an excuse for another wholesale framework rewrite.
