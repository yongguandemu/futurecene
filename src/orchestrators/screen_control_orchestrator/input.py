"""input.py — 鼠标/键盘输入（规格书 5.4 screen:click / screen:keypress）

基于 ctypes SendInput（Win32 原生，无需第三方库），支持 D3D 游戏窗口内输入。

# 模块内容清单（8 项契约）
1. 模块身份标识：screen · input · 能力 screen:click/screen:keypress 输入层
2. 配置契约：无
3. 输入契约：click(x, y, button) / keypress(key, repeat) / type_text(text, interval)
4. 输出契约：bool 成功标记
5. 依赖声明：ctypes、logging、time、typing（Win32 user32 SendInput）
6. 错误定义：未知按键返回 False 并记录警告
7. 生命周期方法：无（模块级函数）
8. 领域状态说明：无（无状态；仅 Win32 常量与 _VK_MAP 键位表）
"""
import ctypes
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Win32 常量（user32）
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
KEYEVENTF_KEYUP = 0x0002
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

user32 = ctypes.windll.user32


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _INPUTUNION)]


def click(x: int, y: int, button: str = "left") -> bool:
    """鼠标点击（绝对坐标）。button: left / right。"""
    user32.SetCursorPos(x, y)
    time.sleep(0.02)
    down_flag = MOUSEEVENTF_LEFTDOWN if button == "left" else MOUSEEVENTF_RIGHTDOWN
    up_flag = MOUSEEVENTF_LEFTUP if button == "left" else MOUSEEVENTF_RIGHTUP
    _send_mouse(down_flag)
    time.sleep(0.03)
    _send_mouse(up_flag)
    logger.info("[Input] click(%d, %d, %s)", x, y, button)
    return True


def keypress(key: str, repeat: int = 1) -> bool:
    """键盘按键（VK 名称，如 "ENTER"/"ESC"/"SPACE" 或单字符）。"""
    vk = _resolve_vk(key)
    if vk is None:
        logger.warning("[Input] 未知按键: %s", key)
        return False
    for _ in range(repeat):
        _send_key(vk, key_up=False)
        time.sleep(0.02)
        _send_key(vk, key_up=True)
        time.sleep(0.02)
    logger.info("[Input] keypress(%s x%d)", key, repeat)
    return True


def type_text(text: str, interval: float = 0.03) -> bool:
    """逐字符输入文本（仅支持单字符键位）。"""
    for ch in text:
        if not keypress(ch, repeat=1):
            logger.warning("[Input] 跳过无法映射字符: %r", ch)
        time.sleep(interval)
    return True


def _send_mouse(flag: int) -> None:
    inp = _INPUT(type=INPUT_MOUSE)
    inp.union.mi.dwFlags = flag
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def _send_key(vk: int, key_up: bool) -> None:
    inp = _INPUT(type=INPUT_KEYBOARD)
    inp.union.ki.wVk = vk
    if key_up:
        inp.union.ki.dwFlags = KEYEVENTF_KEYUP
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


_VK_MAP = {
    "ENTER": 0x0D, "RETURN": 0x0D, "ESC": 0x1B, "ESCAPE": 0x1B,
    "SPACE": 0x20, "TAB": 0x09, "BACKSPACE": 0x08,
    "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
}


def _resolve_vk(key: str) -> Optional[int]:
    k = key.upper()
    if k in _VK_MAP:
        return _VK_MAP[k]
    if len(key) == 1:
        return ord(key.upper())
    return None
