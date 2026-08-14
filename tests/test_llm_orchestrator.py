"""test_llm_orchestrator.py — LLM 调度官单测（mock 外部 API，规格书 1033 行）"""
import asyncio

from src.orchestrators.llm_orchestrator import registry
from src.orchestrators.llm_orchestrator.llm_orchestrator import LLMOrchestrator
from src.shared.event_bus import EventBus
from src.shared.events import LLM_FAILED, LLM_STREAM_CHUNK


class FakeClient:
    """mock 外部 API：可配置回复/失败/流式分片。"""

    engine_name = "fake"

    def __init__(self, reply="fake reply", fail=False, chunks=("你", "好", "呀")):
        self.reply = reply
        self.fail = fail
        self.chunks = chunks
        self.chat_calls = 0

    def chat(self, messages, **kwargs):
        self.chat_calls += 1
        if self.fail:
            raise RuntimeError("provider down")
        return self.reply, {"total_tokens": 10}

    def stream_chat(self, messages, **kwargs):
        if self.fail:
            raise RuntimeError("provider down")
        yield from self.chunks


def _make_orchestrator(primary=None, fallback=None):
    bus = EventBus()
    bus.reset()
    clients = {"openai": primary or FakeClient(), "zhipu": fallback or FakeClient()}
    orch = LLMOrchestrator(event_bus=bus, clients=clients)
    orch.start()
    return orch, bus


def test_capabilities_derived_from_registry():
    orch, _ = _make_orchestrator()
    assert orch.capabilities() == registry.capabilities() == ["llm:chat", "llm:stream_chat"]


def test_chat_success():
    orch, _ = _make_orchestrator(primary=FakeClient(reply="你好呀"))
    result = asyncio.run(orch.handle({"capability": "llm:chat", "payload": {"text": "hi"}}))
    assert result["ok"] is True
    assert result["data"]["reply"] == "你好呀"
    assert result["data"]["usage"]["total_tokens"] == 10
    assert result["error"] is None


def test_chat_fallback_on_primary_failure():
    primary = FakeClient(fail=True)
    fallback = FakeClient(reply="降级回复")
    orch, _ = _make_orchestrator(primary=primary, fallback=fallback)
    result = asyncio.run(orch.handle({"capability": "llm:chat", "payload": {"text": "hi"}}))
    assert result["ok"] is True
    assert result["data"]["reply"] == "降级回复"
    assert fallback.chat_calls == 1


def test_chat_all_failed_returns_fallback_and_publishes_event():
    bus_failures = {}
    orch, bus = _make_orchestrator(primary=FakeClient(fail=True), fallback=FakeClient(fail=True))
    bus.subscribe(LLM_FAILED, lambda event, **kw: bus_failures.update(kw))
    result = asyncio.run(orch.handle({"capability": "llm:chat", "payload": {"text": "hi"}}))
    assert result["ok"] is True
    assert result["data"]["reply"]  # 本地兜底回复非空
    assert result["error"]  # 记录真实错误
    assert bus_failures.get("capability") == "llm:chat"


def test_stream_chat_publishes_chunks():
    bus_chunks = []
    orch, bus = _make_orchestrator(primary=FakeClient(chunks=("你", "好", "呀")))
    bus.subscribe(LLM_STREAM_CHUNK, lambda event, **kw: bus_chunks.append(kw["chunk"]))
    result = asyncio.run(orch.handle({"capability": "llm:stream_chat", "payload": {"text": "hi"}}))
    assert bus_chunks == ["你", "好", "呀"]
    assert result["data"]["reply"] == "你好呀"


def test_build_messages_with_system_and_history():
    orch, _ = _make_orchestrator()
    messages = orch._build_messages({
        "text": "你好",
        "system_prompt": "你是小恶魔",
        "history": [{"role": "user", "content": "前一句"}],
    })
    assert messages[0] == {"role": "system", "content": "你是小恶魔"}
    assert messages[1] == {"role": "user", "content": "前一句"}
    assert messages[-1] == {"role": "user", "content": "你好"}


def test_unknown_capability():
    orch, _ = _make_orchestrator()
    result = asyncio.run(orch.handle({"capability": "llm:unknown", "payload": {}}))
    assert result["ok"] is False
    assert "unknown capability" in result["error"]


def test_health():
    orch, _ = _make_orchestrator()
    assert orch.health()["status"] == "ok"
