"""input_backend.py — 输入注入后端选择（screen 域 · 规格书 5.4 扩展）

按目标窗口分层选择注入后端，解决 SendInput 只能投递前台窗口导致的
「后台采集（PrintWindow）与后台注入矛盾」：
- L1 PostMessage：向目标 hwnd 直发 WM_* 消息，后台/最小化窗口可用（首选）
- L2 游戏桥：已启动的游戏桥（如 MCBridge）内协议执行
- L0 SendInput：前台兜底（input.py 现有实现，已修复批量/DPI）

# 模块内容清单 — input_backend

## 1. 模块身份标识
- 所属调度官：screen
- 能力名：screen:click / screen:keypress / screen:move / screen:drag / screen:scroll（backend 参数扩展）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| 无 | 否 | — | — | 纯函数模块，无独立配置 |

## 3. 输入契约
- dispatch(window_title, action, params, bridge=None) -> Dict
- action ∈ click / double_click / keypress / move / drag / scroll
- params：x/y/key/button/amount/duration 等（与 input.py 对齐）

## 4. 输出契约
- 成功：{"ok": True, "data": {"backend": "postmessage|bridge|sendinput"}}
- 失败：{"ok": False, "error": str}；pywin32 缺失自动降级 L0

## 5. 依赖声明
- 外部服务：无
- 内部模块：input（L0 兜底）
- 预先配置：无（PostMessage 用 win32api/win32gui 可选依赖，缺失降级）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 窗口未找到 | window_title 无匹配 hwnd | 降级 L0 或返回 error |
| 未知动作 | action 不在支持集 | 返回 error |
| pywin32 缺失 | 未安装 win32gui | 自动降级 L0 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | 否 | 纯函数模块，无启动/停止 |

## 8. 领域状态说明
- 状态项：无（无模块级可变状态）
- 持久化：无
- 恢复：无
"""
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Win32 消息常量
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MOUSEWHEEL = 0x020A
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
WHEEL_DELTA = 120

try:
    import win32api
    import win32con
    import win32gui
    _HAS_PYWIN32 = True
except ImportError:
    _HAS_PYWIN32 = False


def _lparam(x: int, y: int) -> int:
    """打包客户区坐标到 lParam（低 16 位 x，高 16 位 y）。"""
    return (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)


def _find_hwnd(window_title: str) -> Optional[int]:
    """按窗口标题找 hwnd；pywin32 缺失或未找到返回 None。"""
    if not _HAS_PYWIN32 or not window_title:
        return None
    try:
        hwnd = win32gui.FindWindow(None, window_title)
        return hwnd or None
    except Exception as e:
        logger.debug("[InputBackend] FindWindow 失败: %s", e)
        return None


def _screen_to_client(hwnd: int, x: int, y: int):
    """屏幕坐标 → 客户区坐标（PostMessage 需要客户区坐标）。"""
    try:
        pt = win32gui.ScreenToClient(hwnd, (int(x), int(y)))
        return pt
    except Exception as e:
        logger.debug("[InputBackend] ScreenToClient 失败: %s", e)
        return (int(x), int(y))


def _post_click(hwnd: int, x: int, y: int, button: str) -> bool:
    cx, cy = _screen_to_client(hwnd, x, y)
    lp = _lparam(cx, cy)
    if button == "right":
        win32api.PostMessage(hwnd, WM_RBUTTONDOWN, MK_RBUTTON, lp)
        win32api.PostMessage(hwnd, WM_RBUTTONUP, 0, lp)
    else:
        win32api.PostMessage(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
        win32api.PostMessage(hwnd, WM_LBUTTONUP, 0, lp)
    logger.info("[InputBackend] postmessage click(%d, %d, %s)", x, y, button)
    return True


def _post_key(hwnd: int, key: str) -> bool:
    from src.orchestrators.screen_control_orchestrator.input import _resolve_vk
    vk = _resolve_vk(key)
    if vk is None:
        logger.warning("[InputBackend] 未知按键: %s", key)
        return False
    win32api.PostMessage(hwnd, WM_KEYDOWN, vk, 1)
    win32api.PostMessage(hwnd, WM_KEYUP, vk, 1 | (1 << 30))
    logger.info("[InputBackend] postmessage keypress(%s)", key)
    return True


def _post_move(hwnd: int, x: int, y: int) -> bool:
    cx, cy = _screen_to_client(hwnd, x, y)
    win32api.PostMessage(hwnd, WM_MOUSEMOVE, 0, _lparam(cx, cy))
    logger.info("[InputBackend] postmessage move(%d, %d)", x, y)
    return True


def _post_wheel(hwnd: int, amount: int) -> bool:
    win32api.PostMessage(hwnd, WM_MOUSEWHEEL, (int(amount) * WHEEL_DELTA) << 16, 0)
    logger.info("[InputBackend] postmessage wheel(%d)", amount)
    return True


def dispatch(window_title: str, action: str, params: Dict[str, Any],
             bridge=None) -> Dict[str, Any]:
    """统一注入入口：按 L1 PostMessage → L2 游戏桥 → L0 SendInput 选择后端。

    Args:
        window_title: 目标窗口标题（L1 需要；空则跳过 L1）
        action: click / double_click / keypress / move / drag / scroll
        params: 动作参数（x/y/key/button/amount/duration 等）
        bridge: 可选游戏桥对象（L2），须有 send(dict) -> bool 接口
    """
    # L1：PostMessage 后台注入（有窗口标题且能解析 hwnd）
    hwnd = _find_hwnd(window_title)
    if hwnd is not None:
        try:
            ok = _dispatch_post(hwnd, action, params)
            if ok:
                return {"ok": True, "data": {"backend": "postmessage"}}
        except Exception as e:
            logger.warning("[InputBackend] PostMessage 失败，降级: %s", e)
    # L2：游戏桥（已启动，游戏内协议执行）
    if bridge is not None and getattr(bridge, "running", False) and hasattr(bridge, "send"):
        try:
            ok = bridge.send({"action": action, "params": params})
            if ok:
                return {"ok": True, "data": {"backend": "bridge"}}
        except Exception as e:
            logger.warning("[InputBackend] 游戏桥失败，降级: %s", e)
    # L0：SendInput 前台兜底
    return _dispatch_sendinput(action, params)


def _dispatch_post(hwnd: int, action: str, params: Dict[str, Any]) -> bool:
    """PostMessage 后端分发（鼠标/键盘消息直发目标窗口）。"""
    if action == "click":
        return _post_click(hwnd, params.get("x", 0), params.get("y", 0),
                           params.get("button", "left"))
    if action == "double_click":
        _post_click(hwnd, params.get("x", 0), params.get("y", 0),
                    params.get("button", "left"))
        return _post_click(hwnd, params.get("x", 0), params.get("y", 0),
                           params.get("button", "left"))
    if action == "keypress":
        return _post_key(hwnd, params.get("key", ""))
    if action == "move":
        return _post_move(hwnd, params.get("x", 0), params.get("y", 0))
    if action == "scroll":
        return _post_wheel(hwnd, params.get("amount", 1))
    if action == "drag":
        # PostMessage 拖拽：按下 → 移动序列 → 抬起
        x1, y1 = params.get("x1", 0), params.get("y1", 0)
        x2, y2 = params.get("x2", 0), params.get("y2", 0)
        _post_click(hwnd, x1, y1, "left")
        _post_move(hwnd, x2, y2)
        cx, cy = _screen_to_client(hwnd, x2, y2)
        win32api.PostMessage(hwnd, WM_LBUTTONUP, 0, _lparam(cx, cy))
        return True
    logger.warning("[InputBackend] 未知动作: %s", action)
    return False


def _dispatch_sendinput(action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """L0 SendInput 兜底（input.py 现有实现）。"""
    from src.orchestrators.screen_control_orchestrator import input as input_mod
    try:
        if action == "click":
            ok = input_mod.click(int(params.get("x", 0)), int(params.get("y", 0)),
                                 params.get("button", "left"))
        elif action == "double_click":
            ok = input_mod.double_click(int(params.get("x", 0)), int(params.get("y", 0)),
                                        params.get("button", "left"))
        elif action == "keypress":
            ok = input_mod.keypress(params.get("key", ""),
                                    int(params.get("repeat", 1)))
        elif action == "move":
            ok = input_mod.move_mouse(int(params.get("x", 0)), int(params.get("y", 0)),
                                      float(params.get("duration", 0.2)))
        elif action == "scroll":
            ok = input_mod.scroll(int(params.get("amount", 1)),
                                  params.get("x"), params.get("y"))
        elif action == "drag":
            ok = input_mod.drag(int(params.get("x1", 0)), int(params.get("y1", 0)),
                                int(params.get("x2", 0)), int(params.get("y2", 0)),
                                float(params.get("duration", 0.5)))
        else:
            return {"ok": False, "error": f"未知动作: {action}"}
        return {"ok": bool(ok), "data": {"backend": "sendinput"}}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def resolve_backend(window_title: str, bridge=None) -> str:
    """探测将选用的后端（供状态查询/日志）：postmessage / bridge / sendinput。"""
    hwnd = _find_hwnd(window_title)
    if hwnd is not None:
        return "postmessage"
    if bridge is not None and getattr(bridge, "running", False) and hasattr(bridge, "send"):
        return "bridge"
    return "sendinput"
