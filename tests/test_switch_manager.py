"""test_switch_manager.py — 开关集中管理（规格书 4.7 + 1152 行日程驱动）"""
from datetime import datetime

from src.commander.switch_manager import SwitchManager
from src.shared.event_bus import EventBus
from src.shared.events import SWITCH_CHANGED


def _make_manager():
    bus = EventBus()
    bus.reset()
    return SwitchManager(bus), bus


def _dt(hour, minute=0):
    return datetime(2026, 8, 13, hour, minute)


def test_auto_register_creates_switch():
    sm, _ = _make_manager()
    sm.auto_register("tts")
    assert sm.is_enabled("tts") is True


def test_auto_register_custom_default():
    sm, _ = _make_manager()
    sm.auto_register("game", default=False)
    assert sm.is_enabled("game") is False


def test_manual_override_priority():
    sm, _ = _make_manager()
    sm.auto_register("tts")  # 默认开启
    sm.set_manual("tts", False)  # 手动禁用 → 优先级最高
    assert sm.is_enabled("tts") is False
    sm.set_manual("tts", True)
    assert sm.is_enabled("tts") is True


def test_clear_manual_restores_default():
    sm, _ = _make_manager()
    sm.auto_register("tts")
    sm.set_manual("tts", False)
    sm.clear_manual("tts")
    assert sm.is_enabled("tts") is True


def test_auto_unregister_removes_switch():
    sm, _ = _make_manager()
    sm.auto_register("tts")
    sm.set_manual("tts", False)
    sm.auto_unregister("tts")
    assert sm.is_enabled("tts") is True  # 回退默认（未注册默认启用）


def test_snapshot():
    sm, _ = _make_manager()
    sm.auto_register("tts")
    sm.auto_register("game", default=False)
    sm.set_manual("tts", False)
    assert sm.snapshot() == {"tts": False, "game": False}


def test_set_manual_publishes_event():
    sm, bus = _make_manager()
    seen = {}
    bus.subscribe(SWITCH_CHANGED, lambda event, **kw: seen.update(kw))
    sm.set_manual("tts", False)
    seen.pop("seq", None)  # seq 为事件元数据，不属于业务载荷
    assert seen == {"name": "tts", "enabled": False, "source": "manual"}


# ---------- 日程驱动 ----------


def test_schedule_enables_within_window():
    sm, _ = _make_manager()
    sm.auto_register("bilibili")
    sm.set_schedule("bilibili", ["09:00-17:00"])
    sm.check_schedules(now=_dt(10, 30))
    assert sm.is_enabled("bilibili") is True
    sm.check_schedules(now=_dt(20, 0))
    assert sm.is_enabled("bilibili") is False


def test_schedule_cross_midnight():
    sm, _ = _make_manager()
    sm.auto_register("bilibili")
    sm.set_schedule("bilibili", ["22:00-02:00"])
    sm.check_schedules(now=_dt(23, 0))
    assert sm.is_enabled("bilibili") is True
    sm.check_schedules(now=_dt(1, 0))
    assert sm.is_enabled("bilibili") is True
    sm.check_schedules(now=_dt(12, 0))
    assert sm.is_enabled("bilibili") is False


def test_manual_overrides_schedule():
    sm, _ = _make_manager()
    sm.auto_register("bilibili")
    sm.set_schedule("bilibili", ["09:00-17:00"])
    sm.set_manual("bilibili", False)  # 手动禁用 → 优先级最高
    sm.check_schedules(now=_dt(10, 0))
    assert sm.is_enabled("bilibili") is False  # 即使日程窗口内仍禁用


def test_schedule_change_publishes_event():
    sm, bus = _make_manager()
    sm.auto_register("bilibili")
    sm.set_schedule("bilibili", ["09:00-17:00"])
    seen = []
    bus.subscribe(SWITCH_CHANGED, lambda event, **kw: seen.append(kw))
    sm.check_schedules(now=_dt(10, 0))  # 变更：禁用→启用
    sm.check_schedules(now=_dt(10, 30))  # 无变更：不重复发布
    sm.check_schedules(now=_dt(20, 0))  # 变更：启用→禁用
    assert len(seen) == 2
    seen[0].pop("seq", None)  # seq 为事件元数据，不属于业务载荷
    assert seen[0] == {"name": "bilibili", "enabled": True, "source": "schedule"}
    assert seen[1]["enabled"] is False


def test_invalid_schedule_format_raises():
    sm, _ = _make_manager()
    try:
        sm.set_schedule("bilibili", ["abc"])
        assert False, "应当抛出 ValueError"
    except ValueError:
        pass
