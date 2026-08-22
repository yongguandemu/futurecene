"""test_p1_pipeline_integration.py — P1 核心链路端到端集成测试（mock 外部 API）

规格书 9.2 验收链路：弹幕 → 记忆检索 → LLM → 字幕 → TTS → tts:audio_ready → Live2D 口型。
ADR-007：管线不含输入/输出安全过滤环节（信任厂商安全系统）。
全部调度官以 Fake 注入（不触真实外部 API），断言编排序列与事件发布。
"""
import asyncio

from src.commander.danmaku_pipeline import DanmakuPipeline
from src.orchestrators.bilibili_orchestrator import normalizer
from src.shared.event_bus import EventBus
from src.shared.events import (
    DANMAKU_RECEIVED,
    FRONTEND_SUBTITLE_UPDATE,
    TTS_AUDIO_READY,
)


class FakeLLM:
    def __init__(self, reply="你好呀！"):
        self.reply = reply
        self.calls = []

    async def handle(self, command):
        self.calls.append(command)
        return {"ok": True, "data": {"reply": self.reply, "usage": {}}, "error": None}


class FakeTTS:
    def __init__(self):
        self.calls = []
        self.event_bus = None  # 由 _make_pipeline 绑定，用于发布 tts:audio_ready

    async def handle(self, command):
        self.calls.append(command)
        if self.event_bus is not None:
            # 模拟真实 TTSOrchestrator：合成后发布 tts:audio_ready
            self.event_bus.publish(TTS_AUDIO_READY, audio_id="tts_fake_001.mp3",
                                   duration_ms=900, path="")
        return {"ok": True, "data": {"audio_id": "tts_fake_001.mp3",
                                     "duration_ms": 900}, "error": None}


class FakeMemory:
    def __init__(self):
        self.retrieve_calls = []
        self.store_calls = []

    async def handle(self, command):
        if command["capability"] == "memory:retrieve":
            self.retrieve_calls.append(command["payload"]["query"])
            return {"ok": True, "data": {"memories": [{"content": "观众喜欢看VN",
                                                       "memory_id": "l1"}]}}
        if command["capability"] == "memory:store":
            self.store_calls.append(command["payload"]["content"])
            return {"ok": True, "data": {"memory_id": "s1"}}
        return {"ok": False, "data": {}, "error": "unknown"}


def _make_pipeline(llm=None, tts=None, memory=None):
    bus = EventBus()
    bus.reset()
    if tts is not None and getattr(tts, "event_bus", None) is None:
        tts.event_bus = bus
    pipe = DanmakuPipeline(event_bus=bus, llm_orchestrator=llm, tts_orchestrator=tts,
                           memory_orchestrator=memory)
    pipe.start()
    return pipe, bus


def _send_danmaku(bus, text="你好呀", user_name="观众"):
    event = normalizer.normalize({"cmd": "DANMU_MSG",
                                  "data": {"text": text, "user_name": user_name,
                                           "user_id": 1}})
    normalizer.publish(bus, event)


def test_full_pipeline_end_to_end():
    """正常链路：记忆注入 → LLM → 字幕 → TTS → audio_ready。"""
    llm, tts, memory = FakeLLM(), FakeTTS(), FakeMemory()
    pipe, bus = _make_pipeline(llm=llm, tts=tts, memory=memory)
    events = []
    bus.subscribe(FRONTEND_SUBTITLE_UPDATE, lambda event, **kw: events.append(("subtitle", kw)))
    bus.subscribe(TTS_AUDIO_READY, lambda event, **kw: events.append(("audio", kw["audio_id"])))

    _send_danmaku(bus)

    # 编排序列断言
    assert memory.retrieve_calls == ["你好呀"]
    assert llm.calls[0]["payload"]["history"] == [{"role": "assistant", "content": "观众喜欢看VN"}]
    assert llm.calls[0]["capability"] == "llm:chat"
    assert tts.calls[0]["payload"]["text"] == "你好呀！"
    assert memory.store_calls == ["你好呀", "你好呀！"]  # 对话入短期记忆
    # 事件断言
    assert events[0][0] == "subtitle" and events[0][1]["text"] == "你好呀！"
    assert events[1][0] == "audio" and events[1][1] == "tts_fake_001.mp3"


def test_chain_with_live2d_lip_sync():
    """完整链路收尾：tts:audio_ready → Live2D 口型同步（表达领域订阅）。"""
    from src.orchestrators.live2d_orchestrator.live2d_orchestrator import Live2DOrchestrator
    llm, tts, memory = FakeLLM(), FakeTTS(), FakeMemory()
    bus = EventBus()
    bus.reset()
    tts.event_bus = bus
    live2d = Live2DOrchestrator(event_bus=bus)
    live2d.start()
    asyncio.run(live2d.handle({"capability": "live2d:load",
                               "payload": {"model_name": "小恶魔"}}))
    pipe = DanmakuPipeline(event_bus=bus, llm_orchestrator=llm, tts_orchestrator=tts,
                           memory_orchestrator=memory)
    pipe.start()

    _send_danmaku(bus)
    # tts:audio_ready 已触发 Live2D 口型同步
    assert live2d.snapshot()["lip_sync"]["audio_id"] == "tts_fake_001.mp3"
    live2d.stop()


def test_system_command_skipped_by_pipeline():
    llm = FakeLLM()
    pipe, bus = _make_pipeline(llm=llm)
    _send_danmaku(bus, text="!点歌 晴天")
    assert llm.calls == []
