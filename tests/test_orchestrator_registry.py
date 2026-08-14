"""test_orchestrator_registry.py — 分 brain 注册表（规格书 4.2，M2 验收 1018 行）"""
import pytest

from src.commander.orchestrator_registry import DuplicateOrchestrator, OrchestratorRegistry
from src.commander.switch_manager import SwitchManager
from src.shared.event_bus import EventBus


class FakeOrchestrator:
    """最小假分 brain：满足 OrchestratorProtocol 的结构。"""

    def __init__(self, name, capabilities):
        self.name = name
        self._capabilities = list(capabilities)
        self.started = False
        self.stopped = False

    def capabilities(self):
        return list(self._capabilities)

    async def handle(self, command):
        return {"ok": True, "data": {}, "error": None}

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def health(self):
        return {"status": "ok", "detail": ""}


@pytest.fixture
def registry():
    bus = EventBus()
    bus.reset()
    sm = SwitchManager(bus)
    return OrchestratorRegistry(sm, bus), sm, bus


def test_register_generates_switch(registry):
    reg, sm, _ = registry
    orch = FakeOrchestrator("tts", ["tts:synthesize"])
    reg.register(orch)
    assert reg.get("tts") is orch
    assert sm.is_enabled("tts") is True
    assert orch.started is True


def test_duplicate_register_raises(registry):
    reg, _, _ = registry
    reg.register(FakeOrchestrator("tts", ["tts:synthesize"]))
    with pytest.raises(DuplicateOrchestrator):
        reg.register(FakeOrchestrator("tts", ["tts:stop"]))


def test_unregister_stops_and_removes(registry):
    reg, sm, _ = registry
    orch = FakeOrchestrator("tts", ["tts:synthesize"])
    reg.register(orch)
    reg.unregister("tts")
    assert orch.stopped is True
    assert reg.get("tts") is None
    assert sm.snapshot() == {}  # 开关项已移除


def test_match_by_capability(registry):
    reg, _, _ = registry
    llm = FakeOrchestrator("llm", ["llm:chat"])
    tts = FakeOrchestrator("tts", ["tts:synthesize", "tts:stop"])
    reg.register(llm)
    reg.register(tts)
    assert reg.match("llm:chat") is llm
    assert reg.match("tts:synthesize") is tts
    assert reg.match("tts:stop") is tts
    assert reg.match("unknown:cap") is None


def test_all_returns_registered(registry):
    reg, _, _ = registry
    reg.register(FakeOrchestrator("a", ["a:x"]))
    reg.register(FakeOrchestrator("b", ["b:y"]))
    assert {o.name for o in reg.all()} == {"a", "b"}
