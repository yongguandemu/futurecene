"""capture.py — 屏幕截图（规格书 5.4 screen:capture）

- capture_screen：mss 全屏/区域截图，保存到 data/cache/，返回路径。
- capture_window：PrintWindow 捕获指定窗口（支持 D3D 游戏窗口，Win32 专用）；
  win32 依赖缺失时自动降级为区域截图。

# 模块内容清单（8 项契约）
1. 模块身份标识：screen · capture · 能力 screen:capture 截图层
2. 配置契约：无（截图保存到 data/cache/）
3. 输入契约：capture_screen(region) / capture_window(window_title, region)
4. 输出契约：图片绝对路径字符串；窗口未找到返回 None
5. 依赖声明：logging、time、pathlib、typing、src.shared.config_loader（PROJECT_ROOT）、mss（可选）、win32gui/win32ui/win32con/PIL（可选）
6. 错误定义：mss 未安装抛 RuntimeError；pywin32 缺失降级为区域截图
7. 生命周期方法：无（模块级函数）
8. 领域状态说明：无（无状态；仅 CACHE_DIR 常量）
"""
import logging
import time
from pathlib import Path
from typing import Optional, Tuple

from src.shared.config_loader import PROJECT_ROOT

logger = logging.getLogger(__name__)

CACHE_DIR = PROJECT_ROOT / "data" / "cache"

try:
    import mss
except ImportError:
    mss = None


def capture_screen(region: Optional[Tuple[int, int, int, int]] = None) -> str:
    """截取屏幕，保存到 data/cache/screen_{ts}.png，返回图片绝对路径。

    Args:
        region: (left, top, width, height)；None 表示全屏。
    """
    if mss is None:
        raise RuntimeError("mss 库未安装，请执行 pip install mss")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"screen_{int(time.time() * 1000)}.png"
    with mss.mss() as sct:
        monitor = {"left": region[0], "top": region[1],
                   "width": region[2], "height": region[3]} if region else sct.monitors[0]
        sct.shot(mon=monitor, output=str(path))
    logger.info("[Capture] 截图完成: %s", path)
    return str(path)


def capture_window(window_title: str,
                   region: Optional[Tuple[int, int, int, int]] = None) -> Optional[str]:
    """PrintWindow 捕获指定窗口（支持 D3D 游戏窗口）。

    # TODO: 确认 — Win32 依赖（pywin32）可选；缺失时降级为区域截图。
    """
    try:
        import win32gui
        import win32ui
        import win32con
        from PIL import Image
    except ImportError:
        logger.warning("[Capture] pywin32 未安装，capture_window 降级为区域截图")
        return capture_screen(region)

    hwnd = win32gui.FindWindow(None, window_title)
    if not hwnd:
        logger.warning("[Capture] 未找到窗口: %s", window_title)
        return None

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width = right - left
    height = bottom - top
    if region:
        left, top, width, height = region

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
    save_dc.SelectObject(bitmap)
    # PrintWindow 捕获含 D3D 内容的窗口
    result = win32gui.PrintWindow(hwnd, save_dc.GetSafeHdc(), win32con.PW_RENDERFULLCONTENT)
    if result == 0:
        logger.warning("[Capture] PrintWindow 失败，回退 BitBlt")
        save_dc.BitBlt((0, 0), (width, height), mfc_dc, (0, 0), win32con.SRCCOPY)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"window_{int(time.time() * 1000)}.png"
    bmpinfo = bitmap.GetInfo()
    bmpdata = bitmap.GetBitmapBits(True)
    img = Image.frombuffer("RGB", (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
                           bmpdata, "raw", "BGRX", 0, 1)
    img.save(str(path))

    win32gui.DeleteObject(bitmap.GetHandle())
    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)
    logger.info("[Capture] 窗口截图完成: %s", path)
    return str(path)
