"""test_screen_control_orchestrator.py — 屏幕控制调度官单测（mock 截图/输入，避免真实操作）"""
import asyncio

from src.orchestrators.screen_control_orchestrator import registry
from src.orchestrators.screen_control_orchestrator.screen_control_orchestrator import (
    ScreenControlOrchestrator,
)
from src.shared.event_bus import EventBus
from src.shared.events import SCREEN_CURSOR_ACTION


class FakeScreenOrchestrator(ScreenControlOrchestrator):
    """注入 mock 的截屏/输入函数，避免 CI 无显示器/真实点击。"""

    def __init__(self, event_bus):
        super().__init__(event_bus)
        self.capture_fn = lambda region=None: "C:/fake/screen.png"
        self.capture_window_fn = lambda title, region=None: "C:/fake/window.png"
        self.click_fn = lambda x, y, button="left": True
        self.keypress_fn = lambda key, repeat=1: True
        self.move_fn = lambda x, y, duration=0.2: True
        self.scroll_fn = lambda amount, x=None, y=None: True
        self.drag_fn = lambda x1, y1, x2, y2, duration=0.5: True
        self.double_click_fn = lambda x, y, button="left": True
        self.ocr_fn = lambda path: "屏幕上的一些文字"
        self.describe_fn = lambda path, key="": "画面描述"
        self.template_match_fn = lambda shot, tpl, threshold=0.8: None


def _make():
    bus = EventBus()
    bus.reset()
    return FakeScreenOrchestrator(bus), bus


def test_capabilities_from_registry():
    orch, _ = _make()
    assert orch.capabilities() == registry.capabilities() == [
        "screen:capture", "screen:click", "screen:keypress", "screen:execute_plan",
        "screen:move", "screen:scroll", "screen:drag", "screen:template_match",
        "screen:cursor", "screen:cursor_state",
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


# ---------- 虚拟光标 ----------

def _make_started():
    orch, bus = _make()
    orch.start()
    return orch, bus


def test_cursor_move_updates_state():
    orch, _ = _make_started()
    try:
        r = asyncio.run(orch.handle({"capability": "screen:cursor",
                                     "payload": {"action": "move", "x": 100, "y": 200,
                                                 "label": "移动"}}))
        assert r["ok"] is True
        state = orch.get_cursor_state("yuki")
        assert state["x"] == 100 and state["y"] == 200
        assert state["label"] == "移动"
    finally:
        orch.stop()


def test_cursor_click_creates_ripple():
    orch, _ = _make_started()
    try:
        asyncio.run(orch.handle({"capability": "screen:cursor",
                                 "payload": {"action": "click", "x": 50, "y": 60}}))
        state = orch.get_cursor_state("yuki")
        assert len(state["ripples"]) >= 1
    finally:
        orch.stop()


def test_cursor_visibility_show_hide():
    orch, _ = _make_started()
    try:
        asyncio.run(orch.handle({"capability": "screen:cursor",
                                 "payload": {"action": "hide", "role": "lilith"}}))
        assert orch.get_cursor_state("lilith")["visible"] is False
        asyncio.run(orch.handle({"capability": "screen:cursor",
                                 "payload": {"action": "show", "role": "lilith"}}))
        assert orch.get_cursor_state("lilith")["visible"] is True
    finally:
        orch.stop()


def test_cursor_active_role_switch():
    orch, _ = _make_started()
    try:
        asyncio.run(orch.handle({"capability": "screen:cursor",
                                 "payload": {"action": "set_active_role", "role": "lilith"}}))
        assert orch.get_cursor_status()["active_role"] == "lilith"
        assert orch.get_cursor_state("lilith")["active"] is True
        assert orch.get_cursor_state("yuki")["active"] is False
    finally:
        orch.stop()


def test_cursor_state_capability():
    orch, _ = _make_started()
    try:
        r = asyncio.run(orch.handle({"capability": "screen:cursor_state", "payload": {}}))
        assert r["ok"] is True
        assert "status" in r["data"]
        assert "cursors" in r["data"]["status"]
        r2 = asyncio.run(orch.handle({"capability": "screen:cursor_state",
                                      "payload": {"role": "yuki"}}))
        assert r2["data"]["cursor"]["role"] == "yuki"
    finally:
        orch.stop()


def test_click_broadcasts_cursor_event():
    orch, bus = _make_started()
    try:
        seen = {}
        bus.subscribe(SCREEN_CURSOR_ACTION,
                      lambda event, **kw: seen.update(kw))
        asyncio.run(orch.handle({"capability": "screen:click",
                                 "payload": {"x": 10, "y": 20}}))
        assert seen.get("action") == "click"
        assert seen.get("x") == 10 and seen.get("y") == 20
    finally:
        orch.stop()


# ---------- 真实鼠标操作（move/scroll/drag/double_click） ----------

def test_move_mouse():
    orch, _ = _make()
    orch.move_fn = lambda x, y, duration=0.2: True
    r = asyncio.run(orch.handle({"capability": "screen:move",
                                 "payload": {"x": 300, "y": 400}}))
    assert r["ok"] is True and r["data"]["x"] == 300


def test_move_missing_coords():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "screen:move", "payload": {}}))
    assert r["ok"] is False


def test_scroll():
    orch, _ = _make()
    orch.scroll_fn = lambda amount, x=None, y=None: True
    r = asyncio.run(orch.handle({"capability": "screen:scroll",
                                 "payload": {"amount": -3}}))
    assert r["ok"] is True


def test_scroll_missing_amount():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "screen:scroll", "payload": {}}))
    assert r["ok"] is False


def test_drag():
    orch, _ = _make()
    orch.drag_fn = lambda x1, y1, x2, y2, duration=0.5: True
    r = asyncio.run(orch.handle({"capability": "screen:drag",
                                 "payload": {"x1": 0, "y1": 0, "x2": 100, "y2": 100}}))
    assert r["ok"] is True


def test_drag_missing_coords():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "screen:drag",
                                 "payload": {"x1": 0, "y1": 0}}))
    assert r["ok"] is False


def test_double_click():
    orch, _ = _make()
    orch.double_click_fn = lambda x, y, button="left": True
    r = asyncio.run(orch.handle({"capability": "screen:double_click",
                                 "payload": {"x": 50, "y": 60}}))
    assert r["ok"] is True


# ---------- 模板匹配 ----------

def test_template_match_found():
    orch, _ = _make()
    orch.template_match_fn = lambda shot, tpl, threshold=0.8: \
        type("M", (), {"x": 120, "y": 80, "confidence": 0.95})()
    r = asyncio.run(orch.handle({"capability": "screen:template_match",
                                 "payload": {"screenshot": "s.png", "template": "t.png"}}))
    assert r["ok"] is True and r["data"]["found"] is True
    assert r["data"]["x"] == 120 and r["data"]["y"] == 80


def test_template_match_not_found():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "screen:template_match",
                                 "payload": {"screenshot": "s.png", "template": "t.png"}}))
    assert r["ok"] is True and r["data"]["found"] is False


def test_template_match_missing_args():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "screen:template_match", "payload": {}}))
    assert r["ok"] is False


def test_execute_plan_with_new_actions():
    orch, _ = _make()
    plan = [{"action": "move", "x": 10, "y": 20},
            {"action": "scroll", "amount": 1},
            {"action": "double_click", "x": 5, "y": 5}]
    r = asyncio.run(orch.handle({"capability": "screen:execute_plan",
                                 "payload": {"plan": plan}}))
    assert r["ok"] is True
    assert len(r["data"]["results"]) == 3
    assert all(res["ok"] for res in r["data"]["results"])
