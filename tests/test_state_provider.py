"""state_provider 测试：快照聚合 + version。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.shared.event_bus import EventBus
from src.web.state_provider import StateProvider


class FakeSession:
    def snapshot(self):
        return {"session_id": "default", "role": "yuki"}


class FakeSwitchManager:
    def snapshot(self):
        return {"llm": True, "tts": False}


class FakeRegistry:
    def all(self):
        return [type("O", (), {"name": "llm"}), type("O", (), {"name": "tts"})]


class FakeDegradation:
    def snapshot(self):
        return {"llm": "normal"}


class FakeMetrics:
    def __call__(self):
        return {"cost": {"total_cost": 0.1}, "watchdog": {"llm": "ok"}}


def test_snapshot_structure():
    bus = EventBus()
    provider = StateProvider(
        event_bus=bus,
        session=FakeSession(),
        switch_manager=FakeSwitchManager(),
        registry=FakeRegistry(),
        degradation_manager=FakeDegradation(),
        metrics_provider=FakeMetrics(),
    )
    snap = provider.snapshot()
    assert set(snap.keys()) == {"version", "session", "switches",
                                "orchestrators", "degradation", "cost", "watchdog"}
    assert snap["session"]["role"] == "yuki"
    assert snap["switches"]["tts"] is False
    assert snap["orchestrators"] == ["llm", "tts"]
    assert isinstance(snap["version"], int)


def test_version_equals_current_seq():
    bus = EventBus()
    provider = StateProvider(event_bus=bus, session=FakeSession(),
                             switch_manager=FakeSwitchManager(),
                             registry=FakeRegistry(),
                             degradation_manager=FakeDegradation(),
                             metrics_provider=FakeMetrics())
    snap = provider.snapshot()
    assert snap["version"] == bus.current_seq()
