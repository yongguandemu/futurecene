"""test_tts_orchestrator.py — TTS 调度官单测（mock dashscope，规格书 1033 行模式）"""
import asyncio

from src.orchestrators.tts_orchestrator import registry
from src.orchestrators.tts_orchestrator.dashscope_client import DashScopeTTSClient
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
    assert seen["role"] == "yuki"  # tts:audio_ready 透传 role（多角色口型路由依赖）
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
    ready = []
    bus.subscribe(TTS_AUDIO_READY,
                  lambda event, **kw: ready.append((kw["audio_id"], kw["role"])))
    r = asyncio.run(orch.handle({"capability": "tts:stream_synthesize",
                                 "payload": {"text": "这是一段较长的文本用于流式分片合成", "role": "yuki"}}))
    assert r["ok"] is True
    assert r["data"]["segments"] >= 1
    assert len(ready) == r["data"]["segments"]
    assert all(role == "yuki" for _, role in ready)  # 每片均透传 role


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


class FakeDashScopeClient(DashScopeTTSClient):
    """模拟 dashscope 客户端：synthesize(text, voice=None) 返回 2 元组（不调用父类构造）。"""

    def __init__(self, audio=b"fake-dashscope", sample_rate=24000):
        self.audio = audio
        self.sample_rate = sample_rate
        self.calls = 0

    def synthesize(self, text, voice=None):
        self.calls += 1
        return self.audio, self.sample_rate


def test_client_synthesize_adapter_dashscope_style():
    """兜底适配：dashscope 客户端用 voice 参数、返回 2 元组，统一为 3 元组契约。"""
    client = FakeDashScopeClient()
    audio, sample_rate, fmt = TTSOrchestrator._client_synthesize(client, "你好", "yuki")
    assert (audio, sample_rate, fmt) == (b"fake-dashscope", 24000, "wav")
    assert client.calls == 1


def test_client_synthesize_adapter_wusound_style():
    """兜底适配：wusound 客户端用 voice_id、返回 3 元组（原样透传）。"""

    class WusoundLike:
        def synthesize(self, text, voice_id=None):
            return b"a", 24000, "mp3"

    audio, sample_rate, fmt = TTSOrchestrator._client_synthesize(WusoundLike(), "hi", "yuki")
    assert (audio, sample_rate, fmt) == (b"a", 24000, "mp3")


def test_synthesize_falls_back_to_dashscope_style():
    """主引擎失败 → 兜底 dashscope 风格客户端成功 → 发布 audio_ready。"""
    primary = FakeTTSClient(fail=True)
    fallback = FakeDashScopeClient()
    bus = EventBus()
    bus.reset()
    orch = TTSOrchestrator(event_bus=bus, client=primary,
                           fallback_client=fallback,
                           cache_dir=str(tmp_path_for_fallback()))
    orch.start()
    seen = {}
    bus.subscribe(TTS_AUDIO_READY, lambda event, **kw: seen.update(kw))
    r = asyncio.run(orch.handle({"capability": "tts:synthesize",
                                 "payload": {"text": "兜底成功", "role": "yuki"}}))
    assert r["ok"] is True
    assert seen.get("audio_id")
    assert fallback.calls == 1


def tmp_path_for_fallback():
    import tempfile
    from pathlib import Path
    return Path(tempfile.mkdtemp()) / "tts_cache"
