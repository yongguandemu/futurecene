"""test_event_bus.py — EventBus 发布/订阅/优先级/历史/未知事件名校验"""
import pytest

from src.shared.event_bus import EventBus, EventPriority
from src.shared.events import COMMAND_RECEIVED, LLM_REQUESTED


@pytest.fixture(autouse=True)
def clean_bus():
    """每个测试前重置单例 EventBus，避免用例间状态串扰。"""
    bus = EventBus()
    bus.reset()
    yield bus


def test_publish_subscribe_delivers_payload(clean_bus):
    received = {}
    clean_bus.subscribe(COMMAND_RECEIVED, lambda event, **kw: received.update(kw))
    clean_bus.publish_sync(COMMAND_RECEIVED, text="你好")
    assert received == {"text": "你好"}


def test_priority_order(clean_bus):
    order = []
    clean_bus.subscribe(
        COMMAND_RECEIVED, lambda event, **kw: order.append("low"), priority=EventPriority.LOW
    )
    clean_bus.subscribe(
        COMMAND_RECEIVED, lambda event, **kw: order.append("high"), priority=EventPriority.HIGHEST
    )
    clean_bus.publish_sync(COMMAND_RECEIVED)
    assert order == ["high", "low"]


def test_history_recorded(clean_bus):
    clean_bus.subscribe(COMMAND_RECEIVED, lambda event, **kw: None)
    clean_bus.publish_sync(COMMAND_RECEIVED, a=1)
    latest = clean_bus.get_latest(COMMAND_RECEIVED)
    assert latest is not None
    assert latest.event == COMMAND_RECEIVED
    assert latest.data == {"a": 1}


def test_unknown_event_raises_on_publish(clean_bus):
    with pytest.raises(ValueError):
        clean_bus.publish("not:registered")


def test_unknown_event_raises_on_subscribe(clean_bus):
    with pytest.raises(ValueError):
        clean_bus.subscribe("not:registered", lambda event, **kw: None)


def test_wildcard_subscribe_allowed(clean_bus):
    # 通配符（llm:*）仅订阅侧放行；注册事件名正常使用
    clean_bus.subscribe("llm:*", lambda event, **kw: None)
    clean_bus.subscribe(LLM_REQUESTED, lambda event, **kw: None)
    assert clean_bus.get_subscriber_count(LLM_REQUESTED)[LLM_REQUESTED] == 1


def test_unsubscribe(clean_bus):
    def handler(event, **kw):
        pass

    clean_bus.subscribe(COMMAND_RECEIVED, handler)
    clean_bus.unsubscribe(COMMAND_RECEIVED, handler)
    assert clean_bus.get_subscriber_count(COMMAND_RECEIVED)[COMMAND_RECEIVED] == 0
