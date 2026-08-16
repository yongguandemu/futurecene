"""test_input_backend.py — 输入注入后端（P1 注入修复）

覆盖：input.py 批量提交（click/keypress 单次 SendInput）、DPI 感知兜底、
input_backend 分层选择（L1 PostMessage → L2 游戏桥 → L0 SendInput）、
screen 调度官 backend 参数透传。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.orchestrators.screen_control_orchestrator import input as input_mod
from src.orchestrators.screen_control_orchestrator import input_backend as backend_mod
from src.orchestrators.screen_control_orchestrator.screen_control_orchestrator import (
    ScreenControlOrchestrator,
)
from src.shared.event_bus import EventBus


# ---------- input.py：批量提交 ----------

def test_input_batch_helpers_exist():
    """批量提交辅助函数存在且可调用（不触发真实输入）。"""
    assert callable(input_mod._send_mouse_batch)
    assert callable(input_mod._send_key_batch)
    assert callable(input_mod.click_fast)
    assert callable(input_mod.keypress_fast)


def test_resolve_vk():
    """按键解析：特殊键 + 单字符。"""
    assert input_mod._resolve_vk("ENTER") == 0x0D
    assert input_mod._resolve_vk("ESC") == 0x1B
    assert input_mod._resolve_vk("A") == ord("A")
    assert input_mod._resolve_vk("unknown_key") is None


# ---------- input_backend.py：后端选择 ----------

def test_backend_resolve_no_pywin32_falls_to_sendinput(monkeypatch):
    """pywin32 缺失时 _find_hwnd 返回 None → resolve 落到 sendinput。"""
    monkeypatch.setattr(backend_mod, "_HAS_PYWIN32", False)
    assert backend_mod._find_hwnd("任意窗口") is None
    assert backend_mod.resolve_backend("任意窗口") == "sendinput"


def test_dispatch_sendinput_fallback(monkeypatch):
    """无窗口标题 + 无桥 → 落到 L0 SendInput（经 input_mod 执行）。"""
    calls = {}

    def fake_click(x, y, button="left"):
        calls["click"] = (x, y, button)
        return True

    monkeypatch.setattr(input_mod, "click", fake_click)
    r = backend_mod.dispatch("", "click", {"x": 10, "y": 20})
    assert r["ok"] is True
    assert r["data"]["backend"] == "sendinput"
    assert calls["click"] == (10, 20, "left")


def test_dispatch_bridge_backend(monkeypatch):
    """游戏桥已启动且无窗口 → L2 游戏桥优先于 L0。"""
    monkeypatch.setattr(backend_mod, "_HAS_PYWIN32", False)
    sent = {}

    class FakeBridge:
        running = True

        def send(self, cmd):
            sent.update(cmd)
            return True

    r = backend_mod.dispatch("", "click", {"x": 1, "y": 2}, bridge=FakeBridge())
    assert r["ok"] is True
    assert r["data"]["backend"] == "bridge"
    assert sent["action"] == "click"


def test_dispatch_unknown_action_sendinput():
    """未知动作返回 error（不抛异常）。"""
    r = backend_mod.dispatch("", "fly", {})
    assert r["ok"] is False
    assert "未知动作" in r["error"]


def test_lparam_packing():
    """lParam 打包：低 16 位 x、高 16 位 y。"""
    lp = backend_mod._lparam(10, 20)
    assert (lp & 0xFFFF) == 10
    assert ((lp >> 16) & 0xFFFF) == 20


# ---------- screen 调度官：backend 参数透传 ----------

class FakeScreen(ScreenControlOrchestrator):
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
    return FakeScreen(bus), bus


def test_click_no_backend_uses_injected_fn():
    """无 backend/window_title → 走注入的 click_fn（兼容现有行为）。"""
    orch, _ = _make()
    calls = []
    orch.click_fn = lambda x, y, button="left": calls.append((x, y, button)) or True
    r = asyncio.run(orch.handle({"capability": "screen:click",
                                 "payload": {"x": 10, "y": 20}}))
    assert r["ok"] is True
    assert calls == [(10, 20, "left")]


def test_click_backend_sendinput_forced():
    """显式 backend=sendinput 强制走注入的 click_fn。"""
    orch, _ = _make()
    calls = []
    orch.click_fn = lambda x, y, button="left": calls.append((x, y, button)) or True
    r = asyncio.run(orch.handle({"capability": "screen:click",
                                 "payload": {"x": 5, "y": 6, "backend": "sendinput"}}))
    assert r["ok"] is True
    assert calls == [(5, 6, "left")]


def test_keypress_backend_sendinput_forced():
    """显式 backend=sendinput 强制走注入的 keypress_fn。"""
    orch, _ = _make()
    calls = []
    orch.keypress_fn = lambda key, repeat=1: calls.append((key, repeat)) or True
    r = asyncio.run(orch.handle({"capability": "screen:keypress",
                                 "payload": {"key": "ENTER", "backend": "sendinput"}}))
    assert r["ok"] is True
    assert calls == [("ENTER", 1)]


def test_click_backend_auto_no_pywin32_falls_back(monkeypatch):
    """window_title 提供但 pywin32 缺失 → auto 降级回注入 fn。"""
    monkeypatch.setattr(backend_mod, "_HAS_PYWIN32", False)
    orch, _ = _make()
    calls = []
    orch.click_fn = lambda x, y, button="left": calls.append((x, y, button)) or True
    r = asyncio.run(orch.handle({"capability": "screen:click",
                                 "payload": {"x": 3, "y": 4,
                                             "window_title": "记事本"}}))
    assert r["ok"] is True
    assert calls == [(3, 4, "left")]


def test_move_drag_scroll_backend_sendinput():
    """move/drag/scroll 显式 sendinput 走注入 fn。"""
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "screen:move",
                                 "payload": {"x": 1, "y": 2, "backend": "sendinput"}}))
    assert r["ok"] is True and r["data"]["x"] == 1
    r = asyncio.run(orch.handle({"capability": "screen:scroll",
                                 "payload": {"amount": 2, "backend": "sendinput"}}))
    assert r["ok"] is True
    r = asyncio.run(orch.handle({"capability": "screen:drag",
                                 "payload": {"x1": 0, "y1": 0, "x2": 1, "y2": 1,
                                             "backend": "sendinput"}}))
    assert r["ok"] is True


def test_double_click_backend_sendinput():
    orch, _ = _make()
    calls = []
    orch.double_click_fn = lambda x, y, button="left": calls.append((x, y, button)) or True
    r = asyncio.run(orch.handle({"capability": "screen:double_click",
                                 "payload": {"x": 7, "y": 8, "backend": "sendinput"}}))
    assert r["ok"] is True
    assert calls == [(7, 8, "left")]


def test_unknown_backend_rejected():
    """非法 backend 值返回 error。"""
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "screen:click",
                                 "payload": {"x": 1, "y": 2, "backend": "magic"}}))
    assert r["ok"] is False
