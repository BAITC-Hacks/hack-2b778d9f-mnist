import wave

import pytest


@pytest.fixture
def short_wav(tmp_path):
    path = tmp_path / "demo.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000)
    return path


@pytest.fixture
def permitted_wav(short_wav):
    return short_wav.read_bytes()
