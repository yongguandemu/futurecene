"""test_watchdog.py — 看门狗 + 降级管理（P5 验收：检测掉线调度官）"""
from src.commander.degradation_manager import DegradationManager
from src.commander.switch_manager import SwitchManager
from src.shared.event_bus import EventBus
from src.shared.watchdog import Watchdog


def _ok_health():
    return {"status": "ok", "detail": "all good"}


def _down_health():
    raise RuntimeError("orchestrator crashed")


def _degraded_health():
    return {"status": "degraded", "detail": "slow"}


def test_watchdog_marks_down_on_exception():
    wd = Watchdog()
    wd.register("llm", _ok_health)
    wd.register("broken", _down_health)
    status = wd.check()
    assert status == {"llm": "ok", "broken": "down"}
    assert wd.is_down("broken") is True
    assert wd.is_down("llm") is False


def test_watchdog_degraded_status():
    wd = Watchdog()
    wd.register("slow", _degraded_health)
    assert wd.check() == {"slow": "degraded"}


def test_watchdog_unregister():
    wd = Watchdog()
    wd.register("llm", _ok_health)
    wd.unregister("llm")
    assert wd.check() == {}


def test_degradation_closes_non_core():
    bus = EventBus()
    bus.reset()
    sm = SwitchManager(bus)
    for name in ("llm", "tts", "game", "screen", "live2d"):
        sm.auto_register(name)
    dm = DegradationManager(sm)
    closed = dm.degrade(reason="cost circuit open")
    assert closed == 3  # game/screen/live2d
    assert sm.is_enabled("game") is False
    assert sm.is_enabled("screen") is False
    assert sm.is_enabled("llm") is True  # 核心不可降级
    assert sm.is_enabled("tts") is True


def test_degradation_restore():
    bus = EventBus()
    bus.reset()
    sm = SwitchManager(bus)
    sm.auto_register("game")
    dm = DegradationManager(sm)
    dm.degrade()
    restored = dm.restore()
    assert restored == 1
    assert sm.is_enabled("game") is True
    assert dm.degraded is False


def test_check_publishes_only_on_flip():
    from src.shared.event_bus import EventBus
    bus = EventBus()
    bus.reset()
    events = []
    bus.subscribe("watchdog:changed", lambda **kw: events.append(kw))
    wd = Watchdog()
    wd._event_bus = bus  # 注入（Watchdog 构造无 event_bus 参数，用属性注入）
    calls = {"n": 0}

    def health():
        calls["n"] += 1
        return {"status": "ok"}

    wd.register("llm", health)
    wd.check()
    wd.check()
    assert len(events) == 0  # 连续 ok 不触发

    # 翻转到 down
    def health_down():
        raise RuntimeError("boom")
    wd.register("llm", health_down)
    wd.check()
    assert len(events) == 1
    assert events[0]["name"] == "llm"
    assert events[0]["status"] == "down"
