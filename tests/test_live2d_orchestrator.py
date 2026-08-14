"""test_live2d_orchestrator.py — Live2D 调度官单测（mock，状态机 + 口型联动）"""
import asyncio

from src.orchestrators.live2d_orchestrator import registry
from src.orchestrators.live2d_orchestrator.live2d_orchestrator import Live2DOrchestrator
from src.shared.event_bus import EventBus
from src.shared.events import (
    LIVE2D_EXPRESSION_CHANGED,
    LIVE2D_LOADED,
    LIVE2D_LIP_SYNC_END,
    LIVE2D_LIP_SYNC_START,
    LIVE2D_MOTION_TRIGGERED,
    TTS_AUDIO_READY,
)


def _make():
    bus = EventBus()
    bus.reset()
    orch = Live2DOrchestrator(event_bus=bus)
    orch.start()
    return orch, bus


def test_capabilities_from_registry():
    orch, _ = _make()
    assert orch.capabilities() == registry.capabilities() == [
        "live2d:load", "live2d:expression", "live2d:motion", "live2d:lip_sync",
    ]


def test_load_model():
    orch, bus = _make()
    seen = {}
    bus.subscribe(LIVE2D_LOADED, lambda event, **kw: seen.update(kw))
    r = asyncio.run(orch.handle({"capability": "live2d:load",
                                 "payload": {"model_name": "小恶魔"}}))
    assert r["ok"] is True and r["data"] == {"loaded": True, "model": "小恶魔"}
    assert seen["model"] == "小恶魔"
    assert orch.snapshot()["model"] == "小恶魔"


def test_expression():
    orch, bus = _make()
    asyncio.run(orch.handle({"capability": "live2d:load",
                             "payload": {"model_name": "小恶魔"}}))
    seen = {}
    bus.subscribe(LIVE2D_EXPRESSION_CHANGED, lambda event, **kw: seen.update(kw))
    r = asyncio.run(orch.handle({"capability": "live2d:expression",
                                 "payload": {"expression": "开心"}}))
    assert r["ok"] is True
    assert seen["expression"] == "开心"


def test_motion():
    orch, bus = _make()
    asyncio.run(orch.handle({"capability": "live2d:load",
                             "payload": {"model_name": "小恶魔"}}))
    seen = {}
    bus.subscribe(LIVE2D_MOTION_TRIGGERED, lambda event, **kw: seen.update(kw))
    r = asyncio.run(orch.handle({"capability": "live2d:motion",
                                 "payload": {"motion": "wave"}}))
    assert r["ok"] is True
    assert seen["motion"] == "wave"


def test_expression_without_model_fails():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "live2d:expression",
                                 "payload": {"expression": "开心"}}))
    assert r["ok"] is False  # 未加载模型时拒绝


def test_lip_sync_on_tts_audio_ready():
    """表达领域协作（规格书 3.4）：tts:audio_ready → 口型同步。"""
    orch, bus = _make()
    asyncio.run(orch.handle({"capability": "live2d:load",
                             "payload": {"model_name": "小恶魔"}}))
    started = []
    ended = []
    bus.subscribe(LIVE2D_LIP_SYNC_START, lambda event, **kw: started.append(kw["audio_id"]))
    bus.subscribe(LIVE2D_LIP_SYNC_END, lambda event, **kw: ended.append(kw["audio_id"]))
    bus.publish(TTS_AUDIO_READY, audio_id="audio-1", duration_ms=200, path="")
    assert started == ["audio-1"]
    assert orch.snapshot()["lip_sync"]["audio_id"] == "audio-1"
    import time
    time.sleep(0.35)  # 等 duration_ms 结束后自动结束口型
    assert ended == ["audio-1"]


def test_unknown_capability():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "live2d:unknown", "payload": {}}))
    assert r["ok"] is False


def test_health():
    orch, _ = _make()
    assert orch.health()["status"] == "ok"
