"""test_live2d_orchestrator.py — Live2D 调度官单测（mock，状态机 + 口型联动 + 参数驱动）"""
import asyncio

from src.orchestrators.live2d_orchestrator import registry
from src.orchestrators.live2d_orchestrator.live2d_orchestrator import Live2DOrchestrator
from src.shared.event_bus import EventBus
from src.shared.events import (
    EMOTION_EXTRACTED,
    LIVE2D_EXPRESSION_CHANGED,
    LIVE2D_LOADED,
    LIVE2D_LIP_SYNC_END,
    LIVE2D_LIP_SYNC_START,
    LIVE2D_MOTION_TRIGGERED,
    LIVE2D_PARAMS_BATCH,
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
        "live2d:emotion", "live2d:params_update",
    ]


def test_emotion_capability_updates_params():
    """live2d:emotion：文本 → 情绪提取 + 参数映射，发布 emotion:extracted。"""
    orch, bus = _make()
    asyncio.run(orch.handle({"capability": "live2d:load",
                             "payload": {"model_name": "Haru", "role": "yuki"}}))
    seen = {}
    bus.subscribe(EMOTION_EXTRACTED, lambda event, **kw: seen.update(kw))
    r = asyncio.run(orch.handle({"capability": "live2d:emotion",
                                 "payload": {"text": "今天好开心啊！", "role": "yuki"}}))
    assert r["ok"] is True
    assert seen.get("emotion") == "开心"
    assert seen.get("role") == "yuki"


def test_params_update_publishes_batch():
    """live2d:params_update：批量参数帧 → 发布 live2d:params_batch。"""
    orch, bus = _make()
    asyncio.run(orch.handle({"capability": "live2d:load",
                             "payload": {"model_name": "Haru", "role": "yuki"}}))
    seen = {}
    bus.subscribe(LIVE2D_PARAMS_BATCH, lambda event, **kw: seen.update(kw))
    r = asyncio.run(orch.handle({"capability": "live2d:params_update",
                                 "payload": {"role": "yuki",
                                             "params": {"ParamEyeLOpen": 0.5}, "ts": 1.0}}))
    assert r["ok"] is True
    assert seen.get("params") == {"ParamEyeLOpen": 0.5}


def test_emotion_requires_loaded_model():
    """未加载模型时 live2d:emotion 返回错误。"""
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "live2d:emotion",
                                 "payload": {"text": "hi"}}))
    assert r["ok"] is False


def test_load_model():
    orch, bus = _make()
    seen = {}
    bus.subscribe(LIVE2D_LOADED, lambda event, **kw: seen.update(kw))
    r = asyncio.run(orch.handle({"capability": "live2d:load",
                                 "payload": {"model_name": "小恶魔"}}))
    assert r["ok"] is True and r["data"] == {"loaded": True, "model": "小恶魔", "role": "yuki"}
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


def test_multi_model_events_carry_role():
    """多角色状态隔离：yuki/lilith 各自独立模型状态且事件携带 role。"""
    orch, bus = _make()
    loaded = []
    bus.subscribe(LIVE2D_LOADED, lambda event, **kw: loaded.append(kw))
    asyncio.run(orch.handle({"capability": "live2d:load",
                             "payload": {"model_name": "Hiyori", "role": "yuki"}}))
    asyncio.run(orch.handle({"capability": "live2d:load",
                             "payload": {"model_name": "小恶魔", "role": "lilith"}}))
    # 两份独立状态且各自 model 正确
    models = orch.snapshot()["models"]
    assert set(models) == {"yuki", "lilith"}
    assert models["yuki"]["model"] == "Hiyori"
    assert models["lilith"]["model"] == "小恶魔"
    assert [e["role"] for e in loaded] == ["yuki", "lilith"]
    # 独立性：yuki 表情变更不影响 lilith 状态
    asyncio.run(orch.handle({"capability": "live2d:expression",
                             "payload": {"expression": "开心", "role": "yuki"}}))
    snap = orch.snapshot()["models"]
    assert snap["yuki"]["expression"] == "开心"
    assert snap["lilith"]["expression"] == "平静"


def test_audio_ready_routes_lip_sync_by_role():
    """tts:audio_ready 按 role 路由口型：仅目标角色收到 START/END，旧线程不误发 END。"""
    orch, bus = _make()
    asyncio.run(orch.handle({"capability": "live2d:load",
                             "payload": {"model_name": "Hiyori", "role": "yuki"}}))
    asyncio.run(orch.handle({"capability": "live2d:load",
                             "payload": {"model_name": "小恶魔", "role": "lilith"}}))
    starts = []
    ends = []
    bus.subscribe(LIVE2D_LIP_SYNC_START, lambda event, **kw: starts.append(kw))
    bus.subscribe(LIVE2D_LIP_SYNC_END, lambda event, **kw: ends.append(kw))

    # 发布 role=lilith 的 tts:audio_ready → 仅 lilith 收到 START（yuki 无）
    bus.publish(TTS_AUDIO_READY, audio_id="a1", duration_ms=200, role="lilith")
    assert starts == [{"audio_id": "a1", "duration_ms": 200, "role": "lilith"}]
    assert all(e["role"] == "lilith" for e in starts)  # yuki 未收到 START

    # 同 role 快速连续口型：a2 覆盖 a1 → 旧线程（a1）不误发 END
    bus.publish(TTS_AUDIO_READY, audio_id="a2", duration_ms=100, role="lilith")
    assert [e["audio_id"] for e in starts] == ["a1", "a2"]

    import time
    time.sleep(0.5)  # 等 a2(100ms) 结束；a1(200ms) 旧线程醒来时已被 a2 覆盖
    assert [e["audio_id"] for e in ends] == ["a2"]
    assert all(e["role"] == "lilith" for e in ends)  # END 携带正确 role
    orch.stop()
