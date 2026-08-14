"""test_tts_orchestrator.py — TTS 调度官单测（mock dashscope，规格书 1033 行模式）"""
import asyncio

from src.orchestrators.tts_orchestrator import registry
from src.orchestrators.tts_orchestrator.tts_orchestrator import TTSOrchestrator
from src.shared.event_bus import EventBus
from src.shared.events import TTS_AUDIO_READY, TTS_FAILED


class FakeTTSClient:
    engine_name = "fake"

    def __init__(self, audio=b"fake-audio-bytes", fail=False, sample_rate=24000):
        self.audio = audio
        self.fail = fail
        self.sample_rate = sample_rate
        self.calls = 0

    def synthesize(self, text, voice_id=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("tts provider down")
        return self.audio, self.sample_rate, "wav"


def _make(tmp_path, client=None, voice_map=None):
    bus = EventBus()
    bus.reset()
    orch = TTSOrchestrator(event_bus=bus, client=client or FakeTTSClient(),
                           cache_dir=str(tmp_path / "tts_cache"),
                           voice_map=voice_map)
    orch.start()
    return orch, bus


def test_capabilities_from_registry(tmp_path):
    orch, _ = _make(tmp_path)
    assert orch.capabilities() == registry.capabilities() == [
        "tts:synthesize", "tts:stream_synthesize", "tts:stop", "tts:cache_clean",
    ]


def test_synthesize_returns_audio_id_and_publishes(tmp_path):
    orch, bus = _make(tmp_path)
    seen = {}
    bus.subscribe(TTS_AUDIO_READY, lambda event, **kw: seen.update(kw))
    r = asyncio.run(orch.handle({"capability": "tts:synthesize",
                                 "payload": {"text": "你好", "role": "yuki"}}))
    assert r["ok"] is True
    assert r["data"]["audio_id"].endswith(".wav")
    assert r["data"]["duration_ms"] > 0
    assert seen["audio_id"] == r["data"]["audio_id"]
    import os
    assert os.path.exists(os.path.join(str(tmp_path / "tts_cache"), r["data"]["audio_id"]))


def test_cache_hit_skips_client(tmp_path):
    client = FakeTTSClient()
    orch, _ = _make(tmp_path, client=client)
    payload = {"text": "缓存测试", "role": "yuki"}
    asyncio.run(orch.handle({"capability": "tts:synthesize", "payload": payload}))
    asyncio.run(orch.handle({"capability": "tts:synthesize", "payload": payload}))
    assert client.calls == 1  # 第二次命中缓存，不重复合成


def test_synthesize_failure_publishes_failed(tmp_path):
    orch, bus = _make(tmp_path, client=FakeTTSClient(fail=True))
    seen = {}
    bus.subscribe(TTS_FAILED, lambda event, **kw: seen.update(kw))
    r = asyncio.run(orch.handle({"capability": "tts:synthesize",
                                 "payload": {"text": "会失败", "role": "yuki"}}))
    assert r["ok"] is False
    assert seen["capability"] == "tts:synthesize"


def test_stream_synthesize_splits_and_publishes(tmp_path):
    orch, bus = _make(tmp_path, client=FakeTTSClient(audio=b"x" * 6000))
    audio_ids = []
    bus.subscribe(TTS_AUDIO_READY, lambda event, **kw: audio_ids.append(kw["audio_id"]))
    r = asyncio.run(orch.handle({"capability": "tts:stream_synthesize",
                                 "payload": {"text": "这是一段较长的文本用于流式分片合成", "role": "yuki"}}))
    assert r["ok"] is True
    assert r["data"]["segments"] >= 1
    assert len(audio_ids) == r["data"]["segments"]


def test_cache_clean(tmp_path):
    orch, _ = _make(tmp_path)
    asyncio.run(orch.handle({"capability": "tts:synthesize",
                             "payload": {"text": "清理我", "role": "yuki"}}))
    r = asyncio.run(orch.handle({"capability": "tts:cache_clean",
                                 "payload": {"max_age_hours": 0}}))
    assert r["ok"] is True
    assert r["data"]["cleaned"] >= 1


def test_unknown_capability(tmp_path):
    orch, _ = _make(tmp_path)
    r = asyncio.run(orch.handle({"capability": "tts:unknown", "payload": {}}))
    assert r["ok"] is False
