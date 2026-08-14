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
    assert orch.handled[0]["payload"] == {"text": "hi"}
    assert events == ["received", "completed"]


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
