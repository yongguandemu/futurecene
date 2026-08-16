"""test_llm_orchestrator.py — LLM 调度官单测（mock 外部 API，规格书 1033 行）"""
import asyncio

from src.orchestrators.llm_orchestrator import registry
from src.orchestrators.llm_orchestrator.llm_orchestrator import LLMOrchestrator
from src.shared.event_bus import EventBus
from src.shared.events import ACTIVE_DIALOGUE, LLM_FAILED, LLM_STREAM_CHUNK


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


# =====================================================================
# 引擎路由（ADR-006 成本控制 + 实测修正）：fast=DeepSeek V4 Flash 优先，pro=DeepSeek V4 Pro 优先，
# zhipu 均作兜底（实测 zhipu 网络不佳时必超时，故 fast 不再以 zhipu 为主引擎）
# =====================================================================

def test_chat_engine_fast_prefers_openai():
    """engine=fast：openai(DeepSeek V4 Flash) 优先，成功时不触达 zhipu。"""
    primary = FakeClient(reply="deepseek")
    fallback = FakeClient(reply="glm-fast")
    orch, _ = _make_orchestrator(primary=primary, fallback=fallback)
    result = asyncio.run(orch.handle({"capability": "llm:chat",
                                      "payload": {"text": "hi", "engine": "fast"}}))
    assert result["data"]["reply"] == "deepseek"
    assert primary.chat_calls == 1 and fallback.chat_calls == 0


def test_chat_engine_fast_falls_back_to_zhipu():
    """engine=fast 且 openai(Flash) 失败 → 降级 zhipu 兜底。"""
    primary = FakeClient(fail=True)
    fallback = FakeClient(reply="glm-fast")
    orch, _ = _make_orchestrator(primary=primary, fallback=fallback)
    result = asyncio.run(orch.handle({"capability": "llm:chat",
                                      "payload": {"text": "hi", "engine": "fast"}}))
    assert result["data"]["reply"] == "glm-fast"
    assert primary.chat_calls == 1 and fallback.chat_calls == 1


def test_chat_engine_fast_uses_dedicated_flash_client():
    """engine=fast 且配置了 _primary_fast（V4 Flash 专用客户端）时优先使用它。"""
    primary = FakeClient(reply="deepseek-pro")
    fast = FakeClient(reply="deepseek-flash")
    fallback = FakeClient(reply="glm-fast")
    bus = EventBus()
    bus.reset()
    clients = {"openai": primary, "zhipu": fallback}
    orch = LLMOrchestrator(event_bus=bus, clients=clients)
    orch.start()
    orch._primary_fast = fast  # 注入 fast 专用客户端（模拟 model_fast 配置）
    result = asyncio.run(orch.handle({"capability": "llm:chat",
                                      "payload": {"text": "hi", "engine": "fast"}}))
    assert result["data"]["reply"] == "deepseek-flash"
    assert fast.chat_calls == 1 and primary.chat_calls == 0


def test_chat_engine_pro_prefers_openai():
    """engine=pro：openai(DeepSeek V4 Pro) 优先，成功时不触达 zhipu。"""
    primary = FakeClient(reply="deepseek")
    fallback = FakeClient(reply="glm")
    orch, _ = _make_orchestrator(primary=primary, fallback=fallback)
    result = asyncio.run(orch.handle({"capability": "llm:chat",
                                      "payload": {"text": "hi", "engine": "pro"}}))
    assert result["data"]["reply"] == "deepseek"
    assert primary.chat_calls == 1 and fallback.chat_calls == 0


def test_chat_default_engine_pro_backward_compat():
    """缺省 engine 保持 openai 优先（向后兼容，pro 安全默认）。"""
    primary = FakeClient(reply="deepseek")
    fallback = FakeClient(reply="glm")
    orch, _ = _make_orchestrator(primary=primary, fallback=fallback)
    result = asyncio.run(orch.handle({"capability": "llm:chat",
                                      "payload": {"text": "hi"}}))
    assert result["data"]["reply"] == "deepseek"
    assert primary.chat_calls == 1 and fallback.chat_calls == 0


def test_stream_chat_engine_fast_prefers_openai():
    """流式同样按 engine 路由：fast 走 openai(Flash) 优先。"""
    primary = FakeClient(chunks=("D", "S"))
    fallback = FakeClient(chunks=("G", "L"))
    orch, _ = _make_orchestrator(primary=primary, fallback=fallback)
    result = asyncio.run(orch.handle({"capability": "llm:stream_chat",
                                      "payload": {"text": "hi", "engine": "fast"}}))
    assert result["data"]["reply"] == "DS"


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


# =====================================================================
# active_dialogue 角色化（Task 18）：tick(role) + role 透传
# =====================================================================

def _make_active(**overrides):
    """构造 ActiveDialogue：冷却 0 / 静默 0 / 概率 1，绕过触发抑制。"""
    from src.orchestrators.llm_orchestrator.active_dialogue import ActiveDialogue
    cfg = {"min_cooldown": 0, "max_silence": 0, "trigger_probability": 1.0}
    cfg.update(overrides)
    bus = EventBus()
    bus.reset()
    ad = ActiveDialogue(event_bus=bus, config=cfg)
    return ad, bus


def test_active_dialogue_tick_role_passthrough():
    """tick(role) 发布 dialogue:active 事件携带 role 字段（Task 18 透传）。"""
    ad, bus = _make_active()
    seen = {}
    bus.subscribe(ACTIVE_DIALOGUE, lambda event, **kw: seen.update(kw))
    ad.tick(role="lilith")
    assert seen.get("role") == "lilith"
    assert seen.get("text")


def test_active_dialogue_tick_default_role_empty():
    """tick() 无角色：事件 role 为空串（单角色兼容，既有行为不变）。"""
    ad, bus = _make_active()
    seen = {}
    bus.subscribe(ACTIVE_DIALOGUE, lambda event, **kw: seen.update(kw))
    ad.tick()
    assert seen.get("role") == ""
    assert seen.get("text")


def test_active_dialogue_role_generator_preferred():
    """set_role_generator(fn(role))：tick(role) 优先调用 role_generator（角色化）。"""
    ad, bus = _make_active()
    got_roles = []
    ad.set_role_generator(lambda role: got_roles.append(role) or
                          {"text": "角色化话题", "mood": "happy"})
    result = ad.tick(role="yuki")
    assert result == {"text": "角色化话题", "mood": "happy"}
    assert got_roles == ["yuki"]


def test_active_dialogue_role_generator_fallback_to_pool():
    """role_generator 抛异常 → 回退话题池（DEFAULT_TOPICS 随机），不中断冷场。"""
    ad, bus = _make_active()
    ad.set_role_generator(lambda role: (_ for _ in ()).throw(RuntimeError("boom")))
    result = ad.tick(role="lilith")
    assert result is not None and result["text"].strip()
    assert result["mood"]
