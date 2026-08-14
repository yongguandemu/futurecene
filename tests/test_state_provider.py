"""state_provider 测试：快照聚合 + version。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.shared.event_bus import EventBus
from src.web.state_provider import StateProvider


class FakeSession:
    def __init__(self, present_roles=()):
        self.present_roles = present_roles

    def snapshot(self):
        snap = {"session_id": "default", "role": "yuki"}
        if self.present_roles:
            snap["present_roles"] = list(self.present_roles)
        return snap


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
                                "orchestrators", "degradation", "cost",
                                "watchdog", "characters"}
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


def test_state_publisher_publishes_on_trigger():
    bus = EventBus()
    bus.reset()
    from src.commander.state_publisher import StatePublisher
    provider = StateProvider(event_bus=bus, session=FakeSession(),
                             switch_manager=FakeSwitchManager(),
                             registry=FakeRegistry(),
                             degradation_manager=FakeDegradation(),
                             metrics_provider=FakeMetrics())
    publisher = StatePublisher(bus, provider)
    publisher.start()
    snapshots = []
    bus.subscribe("state:changed", lambda **kw: snapshots.append(kw))
    # 触发开关变更
    bus.publish("switch:changed", name="llm", enabled=False)
    assert len(snapshots) == 1
    assert "snapshot" in snapshots[0]
    assert snapshots[0]["snapshot"]["version"] <= bus.current_seq()
    # 非触发事件不发布
    bus.publish("llm:requested", text="hi")
    assert len(snapshots) == 1
    publisher.stop()


def test_snapshot_has_characters():
    bus = EventBus()
    provider = StateProvider(event_bus=bus, session=FakeSession(),
                             switch_manager=FakeSwitchManager(),
                             registry=FakeRegistry(),
                             degradation_manager=FakeDegradation(),
                             metrics_provider=FakeMetrics(),
                             characters_provider=lambda: {"yuki": {"present": True}})
    snap = provider.snapshot()
    assert "characters" in snap
    assert snap["characters"]["yuki"]["present"] is True


def test_snapshot_fallback_from_present_roles():
    # 无 characters_provider 时，以 session.present_roles 兜底派生
    bus = EventBus()
    provider = StateProvider(event_bus=bus, session=FakeSession(present_roles=["yuki", "lilith"]),
                             switch_manager=FakeSwitchManager(),
                             registry=FakeRegistry(),
                             degradation_manager=FakeDegradation(),
                             metrics_provider=FakeMetrics())
    snap = provider.snapshot()
    assert snap["characters"] == {"yuki": {"present": True},
                                  "lilith": {"present": True}}


def test_snapshot_empty_characters_without_present_roles():
    # 无 characters_provider 且 session 无 present_roles 时，characters 为空 dict
    bus = EventBus()
    provider = StateProvider(event_bus=bus, session=FakeSession(),
                             switch_manager=FakeSwitchManager(),
                             registry=FakeRegistry(),
                             degradation_manager=FakeDegradation(),
                             metrics_provider=FakeMetrics())
    snap = provider.snapshot()
    assert snap["characters"] == {}


def test_snapshot_empty_characters_from_provider_is_authoritative():
    # provider 明确返回空 dict 时保持权威空值，不触发 present_roles 兜底
    bus = EventBus()
    provider = StateProvider(event_bus=bus, session=FakeSession(present_roles=["yuki"]),
                             switch_manager=FakeSwitchManager(),
                             registry=FakeRegistry(),
                             degradation_manager=FakeDegradation(),
                             metrics_provider=FakeMetrics(),
                             characters_provider=lambda: {})
    snap = provider.snapshot()
    assert snap["characters"] == {}


def test_state_publisher_triggers_on_presence():
    bus = EventBus()
    bus.reset()
    from src.commander.state_publisher import StatePublisher
    provider = StateProvider(event_bus=bus, session=FakeSession(),
                             switch_manager=FakeSwitchManager(),
                             registry=FakeRegistry(),
                             degradation_manager=FakeDegradation(),
                             metrics_provider=FakeMetrics())
    publisher = StatePublisher(bus, provider)
    publisher.start()
    got = []
    bus.subscribe("state:changed", lambda **kw: got.append(kw))
    bus.publish("character:presence_changed", role="lilith", present=True)
    assert len(got) == 1
    publisher.stop()
