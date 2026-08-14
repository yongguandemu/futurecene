"""test_events_schema.py — 事件 schema 校验（规格书 3.3 + M1 验收 992 行）

验收标准：
1. 事件名唯一（无重复值）。
2. 命名格式符合 {domain}:{action}，全小写。
3. ALL_EVENTS 完整收录全部事件常量（唯一注册表一致性）。
4. EventBus 拒绝未注册事件名（schema 校验已集成）。
"""
import re

import pytest

import src.shared.events as events
from src.shared.event_bus import EventBus

EVENT_VALUE_PATTERN = re.compile(r"^[a-z0-9_]+:[a-z0-9_]+$")


def _all_event_constants():
    """收集 events 模块中全部事件常量（大写 str；排除 ALL_EVENTS 等集合）。"""
    return {
        name: value
        for name, value in vars(events).items()
        if name.isupper() and isinstance(value, str)
    }


def test_event_names_unique():
    constants = _all_event_constants()
    values = list(constants.values())
    assert len(values) == len(set(values)), "存在重复事件名"


def test_event_name_format():
    for name, value in _all_event_constants().items():
        assert EVENT_VALUE_PATTERN.match(value), \
            f"{name}={value!r} 不符合 {{domain}}:{{action}} 全小写格式"


def test_all_events_registry_complete():
    constants = _all_event_constants()
    assert set(constants.values()) == set(events.ALL_EVENTS), \
        "ALL_EVENTS 与事件常量不一致"


def test_event_bus_rejects_unregistered():
    bus = EventBus()
    bus.reset()
    with pytest.raises(ValueError):
        bus.publish("bogus:event")
    with pytest.raises(ValueError):
        bus.subscribe("bogus:event", lambda **kw: None)
