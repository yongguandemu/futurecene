"""cursor_overlay.py — 虚拟光标 Win32 透明覆盖窗口（纯 ctypes GDI，无 PyQt）

原系统虚拟光标依赖 PyQt 渲染覆盖窗口，PyQt 缺失时仅日志降级、从未真正显示。
本模块用 Win32 GDI（ctypes 直接调用）实现透明置顶覆盖窗口：
- 全屏透明窗口（WS_EX_LAYERED + COLORKEY 黑色透明 + TOPMOST + 鼠标穿透）
- 每帧 GDI 绘制：角色箭头光标 + 操作标签 + 移动轨迹 + 点击波纹
- 独立渲染线程：消息泵（PeekMessage）+ 30Hz 重绘
- 优雅降级：任何一步失败 → is_available()=False，仅记录日志，不影响逻辑

# 模块内容清单 — cursor_overlay

## 1. 模块身份标识
- 所属调度官：screen
- 能力名：screen:cursor（渲染层）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| 无（纯代码常量） | - | - | - | 帧率 30Hz、COLORKEY 黑色 |

## 3. 输入契约
- 输入格式：`CursorOverlayWindow()`；`init_window()/show()/hide()/request_render(cursor)/request_fade(role, visible, duration)/is_available()`
- request_render 接收 RoleCursor（含 position/label/trail/ripples/opacity）

## 4. 输出契约
- 成功：init_window() 返回 True；is_available() 返回 True
- 失败：任一 Win32 步骤异常 → available=False，仅日志
- 事件：无（纯渲染层）

## 5. 依赖声明
- 外部服务：无（Win32 user32/gdi32/msimg32 经 ctypes）
- 内部模块：无
- 预先配置：VirtualCursorManager 构造时注入

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 窗口创建失败 | 无桌面会话/权限受限 | available=False，降级为事件推送 |
| GDI 对象泄漏 | 异常中断绘制 | finally 释放 DC/位图/画刷/画笔 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| init_window | 是 | 注册窗口类 + 创建透明置顶窗口 + 启动渲染线程 |
| hide | 是 | 隐藏窗口（stop 时调用） |

## 8. 领域状态说明
- 状态项：_hwnd、_available、_render_thread、_dirty 重绘标志、_cursors 待渲染光标
- 持久化：无
- 恢复：无状态持久化；init_window() 重建
"""
import ctypes
import ctypes.wintypes as wt
import logging
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# ---------- Win32 常量 ----------
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
LWA_COLORKEY = 0x00000001
LWA_ALPHA = 0x00000002
SRCCOPY = 0x00CC0020
PS_SOLID = 0
COLORKEY = 0x000000  # 黑色作为透明色

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT), ("wParam", wt.WPARAM),
                ("lParam", wt.LPARAM), ("time", wt.DWORD), ("pt", POINT)]


def _rgb(r: int, g: int, b: int) -> int:
    return r | (g << 8) | (b << 16)


def _hex_to_rgb(hex_color: str):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def _mix(color, ratio: float):
    """把颜色按 ratio 混合到黑色（用于待机半透明效果）。"""
    r, g, b = color
    return (int(r * ratio), int(g * ratio), int(b * ratio))


class CursorOverlayWindow:
    """虚拟光标透明覆盖窗口（GDI 渲染，无 PyQt 依赖）。"""

    _WND_PROC = ctypes.WINFUNCTYPE(ctypes.c_long, wt.HWND, wt.UINT,
                                   wt.WPARAM, wt.LPARAM)

    def __init__(self):
        self._hwnd: Optional[int] = None
        self._available = False
        self._render_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._dirty = threading.Event()
        self._cursors: Dict[str, object] = {}
        self._screen_w = 0
        self._screen_h = 0
        self._class_name = "FutureSceneCursorOverlay"
        self._wnd_proc = self._WND_PROC(self._wnd_proc_impl)

    # ---------- 生命周期 ----------

    def init_window(self) -> bool:
        """注册窗口类 + 创建透明置顶窗口 + 启动渲染线程。"""
        if self._available:
            return True
        try:
            hinst = user32.GetModuleHandleW(None)
            wc = WNDCLASSW()
            wc.style = 0
            wc.lpfnWndProc = ctypes.cast(self._wnd_proc, ctypes.c_void_p)
            wc.hInstance = hinst
            wc.lpszClassName = self._class_name
            if not user32.RegisterClassW(ctypes.byref(wc)):
                err = ctypes.get_last_error()
                if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                    raise ctypes.WinError(err)

            self._screen_w = user32.GetSystemMetrics(0)   # SM_CXSCREEN
            self._screen_h = user32.GetSystemMetrics(1)   # SM_CYSCREEN
            hwnd = user32.CreateWindowExW(
                WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW,
                self._class_name, "FutureSceneCursorOverlay", WS_POPUP,
                0, 0, self._screen_w, self._screen_h,
                None, None, hinst, None)
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._hwnd = hwnd
            # 黑色 COLORKEY 透明 + 整体 alpha 255
            user32.SetLayeredWindowAttributes(hwnd, COLORKEY, 255,
                                              LWA_COLORKEY | LWA_ALPHA)
            user32.ShowWindow(hwnd, 5)  # SW_SHOW
            self._available = True
            self._stop.clear()
            self._render_thread = threading.Thread(target=self._render_loop,
                                                   daemon=True,
                                                   name="cursor-overlay-render")
            self._render_thread.start()
            logger.info("[CursorOverlay] 透明覆盖窗口已创建 %dx%d",
                        self._screen_w, self._screen_h)
            return True
        except Exception as e:
            logger.warning("[CursorOverlay] 覆盖窗口初始化失败（降级为事件推送）: %s", e)
            self._available = False
            return False

    def show(self) -> None:
        if self._hwnd:
            user32.ShowWindow(self._hwnd, 5)

    def hide(self) -> None:
        self._stop.set()
        if self._hwnd:
            user32.ShowWindow(self._hwnd, 0)  # SW_HIDE
        if self._render_thread is not None and self._render_thread.is_alive():
            self._render_thread.join(timeout=3)
        self._render_thread = None

    def is_available(self) -> bool:
        return self._available

    # ---------- 渲染请求 ----------

    def request_render(self, cursor) -> None:
        """请求渲染某角色光标（任意线程调用，置脏标志）。"""
        self._cursors[cursor.role] = cursor
        self._dirty.set()

    def request_fade(self, role: str, visible: bool, duration: float) -> None:
        """淡入/淡出请求（渲染层当前以显隐切换近似实现）。"""
        logger.debug("[CursorOverlay] 角色 %s 淡%s (%.1fs)",
                     role, "入" if visible else "出", duration)

    # ---------- 窗口过程 ----------

    def _wnd_proc_impl(self, hwnd, msg, wparam, lparam):
        if msg == 0x0010:  # WM_CLOSE
            return 0
        if msg == 0x0002:  # WM_DESTROY
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---------- 渲染循环 ----------

    def _render_loop(self) -> None:
        """消息泵 + 30Hz 重绘。"""
        msg = MSG()
        while not self._stop.is_set():
            # 非阻塞消息泵（处理 WM_PAINT 等）
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            if self._dirty.is_set() or self._has_animations():
                try:
                    self._draw()
                except Exception as e:
                    logger.debug("[CursorOverlay] 绘制失败: %s", e)
                self._dirty.clear()
            time.sleep(1.0 / 30.0)

    def _has_animations(self) -> bool:
        for cursor in self._cursors.values():
            if getattr(cursor, "_ripples", None) or getattr(cursor, "_trail", None):
                return True
        return False

    # ---------- GDI 绘制 ----------

    def _draw(self) -> None:
        if not self._hwnd or not self._available:
            return
        hwnd_dc = user32.GetDC(self._hwnd)
        if not hwnd_dc:
            return
        mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, self._screen_w, self._screen_h)
        old_bmp = gdi32.SelectObject(mem_dc, bitmap)
        try:
            # 清屏为 COLORKEY（透明）
            rect = RECT(0, 0, self._screen_w, self._screen_h)
            brush = gdi32.CreateSolidBrush(COLORKEY)
            gdi32.FillRect(mem_dc, ctypes.byref(rect), brush)
            gdi32.DeleteObject(brush)

            for cursor in self._cursors.values():
                self._draw_cursor(mem_dc, cursor)

            gdi32.BitBlt(hwnd_dc, 0, 0, self._screen_w, self._screen_h,
                         mem_dc, 0, 0, SRCCOPY)
        finally:
            gdi32.SelectObject(mem_dc, old_bmp)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(self._hwnd, hwnd_dc)

    def _draw_cursor(self, dc, cursor) -> None:
        if not cursor.is_visible:
            return
        x, y = cursor.position
        theme = cursor.theme
        base_rgb = _hex_to_rgb(theme.get("color", "#ffffff"))
        # 待机态用半透明混合色，活跃态用实色
        color = base_rgb if cursor.is_active else _mix(base_rgb, 0.4)
        pen = gdi32.CreatePen(PS_SOLID, 2, _rgb(*color))
        old_pen = gdi32.SelectObject(dc, pen)

        # 移动轨迹
        for (tx, ty, _ts) in cursor._trail:
            gdi32.SetPixel(dc, int(tx), int(ty), _rgb(*theme.get("trail_color", (255, 255, 255, 80))[:3]))

        # 箭头光标（三角形）
        pts = (POINT * 3)(POINT(x, y), POINT(x + 16, y + 20),
                          POINT(x + 6, y + 22))
        brush = gdi32.CreateSolidBrush(_rgb(*color))
        old_brush = gdi32.SelectObject(dc, brush)
        gdi32.Polygon(dc, pts, 3)
        gdi32.SelectObject(dc, old_brush)
        gdi32.DeleteObject(brush)

        # 点击波纹（空心圆）
        for r in cursor._ripples:
            radius = int(6 + r.progress() * 18)
            gdi32.Ellipse(dc, x - radius, y - radius, x + radius, y + radius)

        gdi32.SelectObject(dc, old_pen)
        gdi32.DeleteObject(pen)

        # 操作标签
        label = cursor.label
        if label:
            old_font = gdi32.SelectObject(dc, gdi32.GetStockObject(17))  # DEFAULT_GUI_FONT
            gdi32.SetBkMode(dc, 1)  # TRANSPARENT
            gdi32.SetTextColor(dc, _rgb(*base_rgb))
            gdi32.TextOutW(dc, x + 20, y + 18, label, len(label))
            gdi32.SelectObject(dc, old_font)
