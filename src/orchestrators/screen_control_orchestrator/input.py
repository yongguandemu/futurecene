"""input.py — 鼠标/键盘输入（规格书 5.4 screen:click / screen:keypress）

基于 ctypes SendInput（Win32 原生，无需第三方库），支持 D3D 游戏窗口内输入。
补齐原系统（screen_executor.py）缺失的真实鼠标控制：
- move_mouse：平滑移动（线性插值，可配时长）
- drag / scroll / double_click：拖拽 / 滚轮 / 双击
- get_cursor_pos / set_cursor_pos：光标位置查询与设置
- 批量 INPUT：click/keypress 合并为单次 SendInput 提交，消除逐次 sleep（低延迟）
- DPI 感知：进程 DPI aware（物理像素），避免缩放系统虚拟化导致坐标偏移

# 模块内容清单（8 项契约）
1. 模块身份标识：screen · input · 能力 screen:click/screen:keypress 输入层
2. 配置契约：无
3. 输入契约：click(x, y, button) / keypress(key, repeat) / type_text(text, interval) /
   move_mouse(x, y, duration) / drag(x1, y1, x2, y2, duration) / scroll(amount) /
   double_click(x, y) / get_cursor_pos() / set_cursor_pos(x, y) /
   click_fast(x, y, button) / keypress_fast(key)
4. 输出契约：bool 成功标记；get_cursor_pos 返回 (x, y) 元组
5. 依赖声明：ctypes、logging、time、typing（Win32 user32 SendInput/SetCursorPos/GetCursorPos/SetProcessDPIAware）
6. 错误定义：未知按键返回 False 并记录警告
7. 生命周期方法：无（模块级函数）
8. 领域状态说明：无（无状态；仅 Win32 常量与 _VK_MAP 键位表）
"""
import ctypes
import ctypes.wintypes
import logging
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Win32 常量（user32）
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_KEYUP = 0x0002
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
WHEEL_DELTA = 120

# DPI 感知级别（shcore SetProcessDpiAwareness）
PROCESS_PER_MONITOR_DPI_AWARE = 2

user32 = ctypes.windll.user32
_DPI_AWARE_INIT = False


def _ensure_dpi_aware() -> None:
    """尽力而为：使进程 DPI aware，坐标使用物理像素（避免 125%/150% 缩放下坐标偏移）。

    仅初始化一次；API 不可用时静默跳过（坐标仍按虚拟像素，不抛错）。
    """
    global _DPI_AWARE_INIT
    if _DPI_AWARE_INIT:
        return
    _DPI_AWARE_INIT = True
    try:
        if hasattr(ctypes.windll, "shcore"):
            shcore = ctypes.windll.shcore
            shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)
        else:
            user32.SetProcessDPIAware()
    except Exception as e:
        logger.debug("[Input] DPI aware 设置失败（坐标按虚拟像素）: %s", e)


_ensure_dpi_aware()


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


def get_cursor_pos() -> Tuple[int, int]:
    """查询当前光标位置（屏幕绝对坐标）。"""
    pt = ctypes.wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


def set_cursor_pos(x: int, y: int) -> bool:
    """直接设置光标位置（无动画）。"""
    return bool(user32.SetCursorPos(int(x), int(y)))


def move_mouse(x: int, y: int, duration: float = 0.2) -> bool:
    """平滑移动鼠标到目标位置（线性插值，duration 秒）。"""
    x, y = int(x), int(y)
    try:
        sx, sy = get_cursor_pos()
    except Exception:
        sx, sy = x, y
    steps = max(1, int(duration / 0.01))
    for i in range(1, steps + 1):
        t = i / steps
        cx = int(sx + (x - sx) * t)
        cy = int(sy + (y - sy) * t)
        user32.SetCursorPos(cx, cy)
        time.sleep(0.01)
    user32.SetCursorPos(x, y)
    logger.info("[Input] move_mouse(%d, %d, %.2fs)", x, y, duration)
    return True


def click(x: int, y: int, button: str = "left") -> bool:
    """鼠标点击（绝对坐标）。button: left / right。

    批量 INPUT：move + down + up 合并为一次 SendInput 提交，单次系统调用完成，
    消除逐次 sleep 的阻塞与抖动。
    """
    _ensure_dpi_aware()
    user32.SetCursorPos(x, y)
    down_flag = MOUSEEVENTF_LEFTDOWN if button == "left" else MOUSEEVENTF_RIGHTDOWN
    up_flag = MOUSEEVENTF_LEFTUP if button == "left" else MOUSEEVENTF_RIGHTUP
    _send_mouse_batch([down_flag, up_flag])
    logger.info("[Input] click(%d, %d, %s)", x, y, button)
    return True


def click_fast(x: int, y: int, button: str = "left") -> bool:
    """极速点击：不做 SetCursorPos 前置，down+up 一次提交（供快环低延迟操作）。"""
    down_flag = MOUSEEVENTF_LEFTDOWN if button == "left" else MOUSEEVENTF_RIGHTDOWN
    up_flag = MOUSEEVENTF_LEFTUP if button == "left" else MOUSEEVENTF_RIGHTUP
    _send_mouse_batch([down_flag, up_flag])
    logger.info("[Input] click_fast(%d, %d, %s)", x, y, button)
    return True


def double_click(x: int, y: int, button: str = "left") -> bool:
    """双击（绝对坐标）。"""
    user32.SetCursorPos(x, y)
    down_flag = MOUSEEVENTF_LEFTDOWN if button == "left" else MOUSEEVENTF_RIGHTDOWN
    up_flag = MOUSEEVENTF_LEFTUP if button == "left" else MOUSEEVENTF_RIGHTUP
    _send_mouse_batch([down_flag, up_flag, down_flag, up_flag])
    logger.info("[Input] double_click(%d, %d, %s)", x, y, button)
    return True


def drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> bool:
    """按住左键从 (x1,y1) 拖拽到 (x2,y2)。"""
    user32.SetCursorPos(x1, y1)
    _send_mouse(MOUSEEVENTF_LEFTDOWN)
    steps = max(1, int(duration / 0.01))
    for i in range(1, steps + 1):
        t = i / steps
        user32.SetCursorPos(int(x1 + (x2 - x1) * t), int(y1 + (y2 - y1) * t))
        time.sleep(0.01)
    user32.SetCursorPos(x2, y2)
    time.sleep(0.03)
    _send_mouse(MOUSEEVENTF_LEFTUP)
    logger.info("[Input] drag(%d,%d -> %d,%d)", x1, y1, x2, y2)
    return True


def scroll(amount: int, x: Optional[int] = None, y: Optional[int] = None) -> bool:
    """滚轮滚动。amount 为正向上、负向下（单位：格）。"""
    if x is not None and y is not None:
        user32.SetCursorPos(x, y)
    _send_mouse_wheel(int(amount * WHEEL_DELTA))
    logger.info("[Input] scroll(%d)", amount)
    return True


def keypress(key: str, repeat: int = 1) -> bool:
    """键盘按键（VK 名称，如 "ENTER"/"ESC"/"SPACE" 或单字符）。

    按下+抬起合并为一次 SendInput 提交（低延迟）。
    """
    vk = _resolve_vk(key)
    if vk is None:
        logger.warning("[Input] 未知按键: %s", key)
        return False
    _send_key_batch([(vk, False), (vk, True)] * repeat)
    logger.info("[Input] keypress(%s x%d)", key, repeat)
    return True


def keypress_fast(key: str) -> bool:
    """极速按键：down+up 一次提交，无多余 sleep（供快环低延迟操作）。"""
    return keypress(key, repeat=1)


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


def _send_mouse_batch(flags) -> None:
    """批量提交鼠标事件（多个 down/up 合并为一次 SendInput 调用）。"""
    if not flags:
        return
    arr = (_INPUT * len(flags))()
    for i, flag in enumerate(flags):
        arr[i].type = INPUT_MOUSE
        arr[i].union.mi.dwFlags = flag
    ctypes.windll.user32.SendInput(len(flags), arr, ctypes.sizeof(_INPUT))


def _send_mouse_wheel(delta: int) -> None:
    inp = _INPUT(type=INPUT_MOUSE)
    inp.union.mi.dwFlags = MOUSEEVENTF_WHEEL
    inp.union.mi.mouseData = ctypes.c_ulong(delta & 0xFFFFFFFF)
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def _send_key(vk: int, key_up: bool = False) -> None:
    inp = _INPUT(type=INPUT_KEYBOARD)
    inp.union.ki.wVk = vk
    if key_up:
        inp.union.ki.dwFlags = KEYEVENTF_KEYUP
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def _send_key_batch(events) -> None:
    """批量提交键盘事件（down/up 对合并为一次 SendInput 调用）。events: [(vk, key_up)]"""
    if not events:
        return
    arr = (_INPUT * len(events))()
    for i, (vk, key_up) in enumerate(events):
        arr[i].type = INPUT_KEYBOARD
        arr[i].union.ki.wVk = vk
        if key_up:
            arr[i].union.ki.dwFlags = KEYEVENTF_KEYUP
    ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))


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
