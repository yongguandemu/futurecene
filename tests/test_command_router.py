"""test_command_router.py — 命令路由（规格书 995 行：未知能力/开关禁用/成功/异常）"""
import asyncio

from src.commander.command_router import CommandRouter
from src.commander.intent_parser import Command
from src.commander.orchestrator_registry import OrchestratorRegistry
from src.commander.switch_manager import SwitchManager
from src.shared.event_bus import EventBus
from src.shared.events import COMMAND_COMPLETED, COMMAND_FAILED, COMMAND_RECEIVED


class FakeOrchestrator:
    def __init__(self, name, capabilities, fail=False):
        self.name = name
        self._capabilities = capabilities
        self.fail = fail
        self.handled = []

    def capabilities(self):
        return list(self._capabilities)

    async def handle(self, command):
        self.handled.append(command)
        if self.fail:
            raise RuntimeError("boom")
        return {"ok": True, "data": {"reply": "ok"}, "error": None}

    def start(self):
        pass

    def stop(self):
        pass

    def health(self):
        return {"status": "ok", "detail": ""}


def _make_router(fail=False):
    bus = EventBus()
    bus.reset()
    sm = SwitchManager(bus)
    reg = OrchestratorRegistry(sm, bus)
    orch = FakeOrchestrator("llm", ["llm:chat"], fail=fail)
    reg.register(orch)
    return CommandRouter(reg, sm, bus), bus, orch, sm


def test_live2d_prepare_orchestrates():
    """live2d:prepare → 编排 load（当前角色）+ 字幕确认（直播间集成联动）。"""
    from src.shared.events import FRONTEND_SUBTITLE_UPDATE
    router, bus, orch, _ = _make_router()
    subtitles = []
    bus.subscribe(FRONTEND_SUBTITLE_UPDATE,
                  lambda event, **kw: subtitles.append(kw.get("text", "")))
    result = asyncio.run(router.dispatch(Command(
        capability="live2d:prepare", payload={},
        source="command", session_id="s1")))
    assert result["ok"] is True
    assert result["data"]["prepared"] is True
    assert any("就绪" in s for s in subtitles)


def test_route_success():
    router, bus, orch, _ = _make_router()
    events = []
    bus.subscribe(COMMAND_RECEIVED, lambda event, **kw: events.append("received"))
    bus.subscribe(COMMAND_COMPLETED, lambda event, **kw: events.append("completed"))
    result = asyncio.run(router.dispatch(Command(capability="llm:chat",
                                                 payload={"text": "hi"},
                                                 source="danmaku",
                                                 session_id="s1")))
    assert result["ok"] is True
    # llm:chat 自动注入 system_prompt（系统能力说明）与 history 兜底
    assert orch.handled[0]["payload"]["text"] == "hi"
    assert "system_prompt" in orch.handled[0]["payload"]
    assert "Future Scene" in orch.handled[0]["payload"]["system_prompt"]
    assert orch.handled[0]["payload"]["history"] == []
    assert events == ["received", "completed"]


def test_llm_injects_role_profile_and_history():
    """llm:chat 注入角色画像 + 系统能力说明 + 前端历史（问题 3/4 修复）。"""
    router, bus, orch, _ = _make_router()
    result = asyncio.run(router.dispatch(Command(
        capability="llm:chat",
        payload={"text": "我该怎么使用这个系统",
                 "history": [{"role": "user", "content": "你好"},
                             {"role": "assistant", "content": "嗨"}]},
        source="command", session_id="s1")))
    assert result["ok"] is True
    payload = orch.handled[0]["payload"]
    assert payload["history"] == [{"role": "user", "content": "你好"},
                                  {"role": "assistant", "content": "嗨"}]
    sp = payload["system_prompt"]
    assert "Future Scene" in sp and "查看系统状态" in sp and "切换角色" in sp


def test_unknown_capability():
    router, bus, orch, _ = _make_router()
    result = asyncio.run(router.dispatch(Command(capability="unknown:x",
                                                 payload={}, source="danmaku",
                                                 session_id="s1")))
    assert result["ok"] is False
    assert "unknown capability" in result["error"]
    assert orch.handled == []  # 未路由


def test_switch_disabled_intercepts():
    router, bus, orch, sm = _make_router()
    sm.set_manual("llm", False)  # 手动禁用
    result = asyncio.run(router.dispatch(Command(capability="llm:chat",
                                                 payload={}, source="danmaku",
                                                 session_id="s1")))
    assert result["ok"] is False
    assert "orchestrator disabled" in result["error"]
    assert orch.handled == []  # 开关拦截，不调用 handle


def test_handle_exception_publishes_failed():
    router, bus, orch, _ = _make_router(fail=True)
    events = []
    bus.subscribe(COMMAND_FAILED, lambda event, **kw: events.append(kw["error"]))
    result = asyncio.run(router.dispatch(Command(capability="llm:chat",
                                                 payload={}, source="danmaku",
                                                 session_id="s1")))
    assert result["ok"] is False
    assert "boom" in result["error"]
    assert "boom" in events[0]


def test_dispatch_publishes_command_id():
    bus = EventBus()
    bus.reset()
    received = []
    bus.subscribe("commander:command_received", lambda **kw: received.append(kw))
    bus.subscribe("commander:command_completed", lambda **kw: received.append(kw))
    orch = FakeOrchestrator("llm", ["llm:chat"])  # 既有测试中的 stub，capability="llm:chat"
    sm = SwitchManager(bus)
    reg = OrchestratorRegistry(sm, bus)
    reg.register(orch)
    router = CommandRouter(reg, sm, bus)
    cmd = Command(capability="llm:chat", payload={}, source="command",
                  session_id="default")
    result = asyncio.run(router.dispatch(cmd))
    assert result["ok"] is True
    cids = [kw.get("command_id") for kw in received if kw.get("command_id")]
    assert len(cids) == 2
    assert cids[0] == cids[1]
    assert len(cids[0]) == 32  # uuid4().hex


def test_existing_command_id_preserved():
    bus = EventBus()
    bus.reset()
    seen = {}
    bus.subscribe("commander:command_completed", lambda **kw: seen.update(kw))
    orch = FakeOrchestrator("llm", ["llm:chat"])
    sm = SwitchManager(bus)
    reg = OrchestratorRegistry(sm, bus)
    reg.register(orch)
    router = CommandRouter(reg, sm, bus)
    cmd = Command(capability="llm:chat", payload={}, source="command",
                  session_id="default", command_id="predefined-123")
    asyncio.run(router.dispatch(cmd))
    assert seen.get("command_id") == "predefined-123"
