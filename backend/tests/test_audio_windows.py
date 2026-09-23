import pytest

from app.audio import _speech_windows, transcribe_turns
from app.process import process_recording


def test_long_single_speaker_input_is_always_short_form():
    assert _speech_windows(65, [(0, 65, "a")]) == [(0, 25), (25, 50), (50, 65)]


def test_overlap_is_one_audio_window_and_voice_changes_keep_their_boundary():
    windows = _speech_windows(12, [(1, 7, "a"), (5, 10, "b")])
    assert windows == [(1, 5), (5, 7), (7, 10)]
    assert sum(end - start for start, end in windows) == 9


def test_short_pause_of_one_voice_is_preserved_within_the_window():
    assert _speech_windows(10, [(0, 3, "a"), (3.2, 6, "a"), (7, 9, "b")]) == [
        (0, 6), (7, 9),
    ]


def test_empty_diarization_produces_no_hallucinated_silence_transcript():
    assert _speech_windows(10, []) == []


def test_standalone_recognition_uses_bounded_windows_without_diarization():
    assert _speech_windows(32) == [(0, 25), (25, 32)]


@pytest.mark.parametrize("interval", [(0, float("nan"), "a"), (4, 2, "a")])
def test_invalid_diarization_is_rejected(interval):
    with pytest.raises(ValueError, match="diarization interval"):
        _speech_windows(10, [interval])


def test_float_tail_cannot_create_a_zero_sample_asr_window():
    windows = _speech_windows(30, [(0.08, 25.080000000000002, "a")])
    assert windows == [(0.08, 25.08)]
    assert [round(end * 16000) - round(start * 16000) for start, end in windows] == [400000]


def test_tiny_overlap_is_kept_in_one_uncertain_window(short_wav):
    intervals = [(0, 0.06, "a"), (0.04, 1.0, "b")]
    windows = _speech_windows(1, intervals)
    assert windows == [(0.0, 1.0)]
    result = process_recording(short_wav, "2026-09-23", [],
                               lambda _: [{"start": start, "end": end, "text": "Реплика"}
                                          for start, end in windows],
                               lambda _: intervals, lambda *_: {"summary": "", "actions": []})
    assert result["transcript"][0]["overlap"] is True
    assert result["transcript"][0]["speaker_uncertain"] is True
    assert result["transcript"][0]["speaker_id"] is None


def test_short_residual_is_rebalanced_within_maximum_input_length():
    windows = _speech_windows(26, [(0, 25.03, "a")])
    assert len(windows) == 2
    assert windows[0][0] == 0
    assert windows[0][1] == windows[1][0]
    assert windows[1][1] == 25.03
    assert all(0.1 <= end - start <= 25 for start, end in windows)


def test_recording_with_only_micro_fragments_never_loads_asr(short_wav, tmp_path, monkeypatch):
    monkeypatch.setenv("ASR_MODEL_PATH", str(tmp_path))
    assert transcribe_turns(short_wav, [(0.2, 0.24, "a")]) == []


def test_subsecond_diarization_fragments_receive_neighboring_context():
    # Real 350–410 ms boundaries produced unrelated subtitle text on track-2.
    # Keep their audio, with context, and let process_recording mark mixed voices.
    windows = _speech_windows(10, [(0, 4, "a"), (4.1, 4.45, "b"), (4.5, 9, "a")])
    assert windows == [(0, 4.45), (4.5, 9)]
