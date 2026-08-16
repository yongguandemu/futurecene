"""test_local_player.py — 本机 TTS 播放器（格式守卫：WAV 头校验）

覆盖：真实 WAV 播放、MP3 伪装 .wav 跳过（修复：winsound 提示音）、
文件缺失跳过。winsound 调用打桩，不真实发声。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.orchestrators.tts_orchestrator.local_player import LocalTTSSpeaker
from src.shared.event_bus import EventBus
from src.shared.events import TTS_AUDIO_READY


@pytest.fixture
def speaker(tmp_path):
    bus = EventBus()
    bus.reset()
    sp = LocalTTSSpeaker(bus, cache_dir=str(tmp_path))
    sp.start()
    return sp, bus, tmp_path


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def test_is_wav_detects_real_riff(speaker):
    _, _, tmp = speaker
    p = tmp / "a.wav"
    _write(p, b"RIFFxxxx")
    assert LocalTTSSpeaker._is_wav(p) is True


def test_is_wav_rejects_mp3_disguised(speaker):
    """修复：MP3 伪装 .wav（ID3 头）被识别为非 WAV，跳过播放（不再触发系统提示音）。"""
    _, _, tmp = speaker
    p = tmp / "fake.wav"
    _write(p, b"ID3\x04\x00\x00\x00fake-mp3-content")
    assert LocalTTSSpeaker._is_wav(p) is False


def test_play_wav_calls_winsound(speaker, monkeypatch):
    sp, bus, tmp = speaker
    p = tmp / "real.wav"
    _write(p, b"RIFFwav-data")
    played = []
    monkeypatch.setattr("winsound.PlaySound",
                        lambda path, flags: played.append(path))
    bus.publish(TTS_AUDIO_READY, audio_id="real.wav", role="yuki")
    assert sp.wait_played(timeout=1.0) is True
    assert played and played[0].endswith("real.wav")


def test_skip_mp3_disguised_wav(speaker, monkeypatch):
    """修复：非 WAV 文件跳过播放，winsound 不被调用（无提示音）。"""
    sp, bus, tmp = speaker
    p = tmp / "fake.wav"
    _write(p, b"ID3fake-mp3")
    played = []
    monkeypatch.setattr("winsound.PlaySound",
                        lambda path, flags: played.append(path))
    bus.publish(TTS_AUDIO_READY, audio_id="fake.wav", role="yuki")
    import time
    time.sleep(0.2)
    assert played == []


def test_missing_file_skipped(speaker, monkeypatch):
    sp, bus, _ = speaker
    played = []
    monkeypatch.setattr("winsound.PlaySound",
                        lambda path, flags: played.append(path))
    bus.publish(TTS_AUDIO_READY, audio_id="not_exist.wav", role="yuki")
    import time
    time.sleep(0.2)
    assert played == []
