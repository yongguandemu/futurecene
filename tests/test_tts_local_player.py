"""本机 TTS 播放器单测（mock winsound，不碰真实声卡）。"""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest import mock

from src.orchestrators.tts_orchestrator.local_player import LocalTTSSpeaker
from src.shared.event_bus import EventBus
from src.shared.events import TTS_AUDIO_READY


def _make(cache_dir):
    bus = EventBus()
    bus.reset()
    sp = LocalTTSSpeaker(bus, cache_dir=str(cache_dir))
    return bus, sp


def test_start_plays_audio_on_ready(tmp_path):
    (tmp_path / "tts_x.wav").write_bytes(b"RIFF fake wav")
    bus, sp = _make(tmp_path)
    sp.start()
    with mock.patch("src.orchestrators.tts_orchestrator.local_player.winsound.PlaySound") as ps:
        bus.publish(TTS_AUDIO_READY, audio_id="tts_x.wav")
        assert sp.wait_played()
    ps.assert_called_once()
    args, kwargs = ps.call_args
    assert str(args[0]).endswith("tts_x.wav")        # 播放缓存目录下的该文件
    assert args[1] & 0x0001                           # SND_FILENAME 标志位
    sp.stop()


def test_missing_file_no_play(tmp_path):
    bus, sp = _make(tmp_path)
    sp.start()
    with mock.patch("src.orchestrators.tts_orchestrator.local_player.winsound.PlaySound") as ps:
        bus.publish(TTS_AUDIO_READY, audio_id="nope.wav")
        time.sleep(0.1)                                # 线程窗口
    ps.assert_not_called()
    sp.stop()


def test_stop_unsubscribes(tmp_path):
    (tmp_path / "tts_x.wav").write_bytes(b"RIFF fake wav")
    bus, sp = _make(tmp_path)
    sp.start()
    sp.stop()
    with mock.patch("src.orchestrators.tts_orchestrator.local_player.winsound.PlaySound") as ps:
        bus.publish(TTS_AUDIO_READY, audio_id="tts_x.wav")
        time.sleep(0.1)
    ps.assert_not_called()


def test_play_failure_logs_no_crash(tmp_path):
    (tmp_path / "tts_x.wav").write_bytes(b"RIFF fake wav")
    bus, sp = _make(tmp_path)
    sp.start()
    with mock.patch("src.orchestrators.tts_orchestrator.local_player.winsound.PlaySound",
                    side_effect=OSError("no audio device")):
        bus.publish(TTS_AUDIO_READY, audio_id="tts_x.wav")
        # 不崩溃即可；播放计数不增长
        time.sleep(0.1)
    assert sp._play_count == 0
    sp.stop()
