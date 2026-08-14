"""session_context 在场模型测试。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.shared.event_bus import EventBus
from src.commander.session_context import SessionContext


def test_presence_model():
    bus = EventBus()
    session = SessionContext(session_id="default")
    session.bind_event_bus(bus)
    events = []
    bus.subscribe("character:presence_changed", lambda **kw: events.append(kw))

    assert session.present_roles == {"yuki"}
    session.add_role("lilith")
    assert session.present_roles == {"yuki", "lilith"}
    assert len(events) == 1 and events[0]["role"] == "lilith"

    session.set_lead("lilith")
    assert session.lead_role == "lilith"

    session.remove_role("lilith")
    assert session.present_roles == {"yuki"}


def test_remove_last_role_safe():
    """移除唯一在场角色：不抛异常、返回 True、present_roles 为空、role/lead_role 回退 yuki。"""
    session = SessionContext(session_id="default")
    assert session.remove_role("yuki") is True
    assert session.present_roles == set()
    assert session.role == "yuki"
    assert session.lead_role == "yuki"


def test_switch_role_compat():
    bus = EventBus()
    session = SessionContext(session_id="default")
    session.bind_event_bus(bus)
    session.add_role("lilith")
    assert session.switch_role("lilith") is True   # 兼容：设焦点角色
    assert session.role == "lilith"
    assert session.snapshot()["role"] == "lilith"
    assert set(session.snapshot()["present_roles"]) == {"yuki", "lilith"}
    assert session.switch_role("nobody") is False
