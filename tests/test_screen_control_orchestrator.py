"""test_screen_control_orchestrator.py — 屏幕控制调度官单测（mock 截图/输入，避免真实操作）"""
import asyncio

from src.orchestrators.screen_control_orchestrator import registry
from src.orchestrators.screen_control_orchestrator.screen_control_orchestrator import (
    ScreenControlOrchestrator,
)
from src.shared.event_bus import EventBus


class FakeScreenOrchestrator(ScreenControlOrchestrator):
    """注入 mock 的截屏/输入函数，避免 CI 无显示器/真实点击。"""

    def __init__(self, event_bus):
        super().__init__(event_bus)
        self.capture_fn = lambda region=None: "C:/fake/screen.png"
        self.capture_window_fn = lambda title, region=None: "C:/fake/window.png"
        self.click_fn = lambda x, y, button="left": True
        self.keypress_fn = lambda key, repeat=1: True
        self.ocr_fn = lambda path: "屏幕上的一些文字"
        self.describe_fn = lambda path, key="": "画面描述"


def _make():
    bus = EventBus()
    bus.reset()
    return FakeScreenOrchestrator(bus), bus


def test_capabilities_from_registry():
    orch, _ = _make()
    assert orch.capabilities() == registry.capabilities() == [
        "screen:capture", "screen:click", "screen:keypress", "screen:execute_plan",
    ]


def test_capture_returns_image_path():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "screen:capture", "payload": {}}))
    assert r["ok"] is True
    assert r["data"]["image_path"] == "C:/fake/screen.png"


def test_capture_with_ocr_and_description():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "screen:capture",
                                 "payload": {"with_ocr": True, "with_description": True}}))
    assert r["data"]["text"] == "屏幕上的一些文字"
    assert r["data"]["description"] == "画面描述"


def test_capture_window_not_found():
    orch, _ = _make()
    orch.capture_window_fn = lambda title, region=None: None
    r = asyncio.run(orch.handle({"capability": "screen:capture",
                                 "payload": {"window_title": "不存在的窗口"}}))
    assert r["ok"] is False


def test_click():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "screen:click", "payload": {"x": 100, "y": 200}}))
    assert r["ok"] is True and r["data"] == {"done": True}


def test_click_missing_coords():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "screen:click", "payload": {}}))
    assert r["ok"] is False


def test_keypress():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "screen:keypress", "payload": {"key": "ENTER"}}))
    assert r["ok"] is True


def test_execute_plan_sequence():
    orch, _ = _make()
    plan = [{"action": "click", "x": 10, "y": 20},
            {"action": "wait", "seconds": 0.01},
            {"action": "capture"}]
    r = asyncio.run(orch.handle({"capability": "screen:execute_plan", "payload": {"plan": plan}}))
    assert r["ok"] is True
    assert len(r["data"]["results"]) == 3
    assert r["data"]["results"][0]["action"] == "click"
    assert r["data"]["results"][1]["action"] == "wait"


def test_unknown_capability():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "screen:unknown", "payload": {}}))
    assert r["ok"] is False
