"""screen_control_orchestrator.py — 屏幕控制调度官主类（规格书 5.4）

能力：screen:capture / screen:click / screen:keypress / screen:execute_plan /
screen:move / screen:scroll / screen:drag / screen:template_match /
screen:cursor / screen:cursor_state。
输入注入分层（P1 注入修复）：payload 支持 backend=auto|postmessage|sendinput 与
window_title；auto 时经 input_backend 按 L1 PostMessage（后台窗口）→ L2 游戏桥
→ L0 SendInput（前台兜底）选择后端，解决「后台采集与前台注入矛盾」。
职责边界（5.5）：不主动截屏（除游戏实况调度官内部定时外），只响应命令调用。
虚拟光标：输入层执行后经 EventBus 广播 screen:cursor_action，VirtualCursorManager
订阅后更新双角色光标状态并渲染（Win32 覆盖窗口 + 前端事件）。

# 模块内容清单（8 项契约）
1. 模块身份标识：screen · ScreenControlOrchestrator · 能力 screen:capture/click/keypress/execute_plan/move/scroll/drag/template_match/cursor/cursor_state
2. 配置契约：vision_api_key 视觉模型密钥（可选）
3. 输入契约：handle(command) 指令字典（capability + payload：region/window_title/x/y/key/plan/action/label/role/visible/template/threshold/backend/bridge 等）
4. 输出契约：{ok, data, error} 响应字典；execute_plan 返回逐步结果列表；cursor_state 返回光标状态；backend 注入返回实际后端（postmessage/bridge/sendinput）
5. 依赖声明：logging、typing、registry、capture、input、input_backend、vision、template_match、virtual_cursor、cursor_overlay 模块
6. 错误定义：截屏/点击/按键异常捕获并返回 error；x/y/key/plan 缺失返回 error；PostMessage 窗口未找到返回 error
7. 生命周期方法：start()/stop()/health()；virtual_cursor 随 start/stop 启停
8. 领域状态说明：_started 启动标记；_vision_api_key；capture/click/keypress/ocr/describe/move/scroll/drag/template_match 函数注入点；_virtual_cursor 虚拟光标管理器
"""
import logging
from typing import Any, Dict, List, Optional

from src.orchestrators.screen_control_orchestrator import registry
from src.orchestrators.screen_control_orchestrator import capture as capture_mod
from src.orchestrators.screen_control_orchestrator import input as input_mod
from src.orchestrators.screen_control_orchestrator import input_backend as input_backend_mod
from src.orchestrators.screen_control_orchestrator import template_match as template_match_mod
from src.orchestrators.screen_control_orchestrator import vision as vision_mod
from src.orchestrators.screen_control_orchestrator.cursor_overlay import CursorOverlayWindow
from src.orchestrators.screen_control_orchestrator.virtual_cursor import VirtualCursorManager
from src.shared.events import (
    SCREEN_CURSOR_ACTION,
    SCREEN_CURSOR_ACTIVE_ROLE,
    SCREEN_CURSOR_VISIBILITY,
)

logger = logging.getLogger(__name__)


class ScreenControlOrchestrator:
    """屏幕控制调度官。"""

    name = "screen"

    def __init__(self, event_bus, vision_api_key: str = ""):
        self._event_bus = event_bus
        self._vision_api_key = vision_api_key
        self._started = False
        # 依赖注入点（测试/CI 无显示器场景）
        self.capture_fn = capture_mod.capture_screen
        self.capture_window_fn = capture_mod.capture_window
        self.click_fn = input_mod.click
        self.keypress_fn = input_mod.keypress
        self.move_fn = input_mod.move_mouse
        self.scroll_fn = input_mod.scroll
        self.drag_fn = input_mod.drag
        self.double_click_fn = input_mod.double_click
        self.ocr_fn = vision_mod.ocr
        self.describe_fn = vision_mod.describe
        self.template_match_fn = template_match_mod.find_template
        # 虚拟光标（双角色状态管理 + Win32 覆盖窗口渲染）
        self._overlay = CursorOverlayWindow()
        self._virtual_cursor = VirtualCursorManager(event_bus=event_bus,
                                                    overlay=self._overlay)
        registry.bind(self.handle)

    def capabilities(self) -> List[str]:
        return registry.capabilities()

    def start(self) -> None:
        self._started = True
        self._virtual_cursor.start()
        logger.info("[ScreenControlOrchestrator] 已启动（虚拟光标已就绪）")

    async def handle(self, command: Dict[str, Any]) -> Dict[str, Any]:
        capability = command.get("capability", "")
        payload = command.get("payload", {})
        if capability == "screen:capture":
            return self._capture(payload)
        if capability == "screen:click":
            return self._click(payload)
        if capability == "screen:keypress":
            return self._keypress(payload)
        if capability == "screen:execute_plan":
            return self._execute_plan(payload)
        if capability == "screen:move":
            return self._move(payload)
        if capability == "screen:scroll":
            return self._scroll(payload)
        if capability == "screen:drag":
            return self._drag(payload)
        if capability == "screen:template_match":
            return self._template_match(payload)
        if capability == "screen:cursor":
            return self._cursor(payload)
        if capability == "screen:cursor_state":
            return self._cursor_state(payload)
        if capability == "screen:double_click":
            return self._double_click(payload)
        return {"ok": False, "data": {}, "error": f"unknown capability: {capability}"}

    def health(self) -> Dict[str, Any]:
        return {"status": "ok" if self._started else "down", "detail": ""}

    def stop(self) -> None:
        self._virtual_cursor.stop()
        self._started = False

    # ---------- 内部实现 ----------

    def _capture(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        region = payload.get("region")
        window_title = payload.get("window_title")
        try:
            if window_title:
                image_path = self.capture_window_fn(window_title, region)
                if image_path is None:
                    return {"ok": False, "data": {}, "error": f"窗口未找到: {window_title}"}
            else:
                image_path = self.capture_fn(region)
            result: Dict[str, Any] = {"image_path": image_path}
            if payload.get("with_ocr"):
                result["text"] = self.ocr_fn(image_path)
            if payload.get("with_description"):
                result["description"] = self.describe_fn(image_path, self._vision_api_key)
            return {"ok": True, "data": result, "error": None}
        except Exception as e:
            logger.error("[ScreenControl] 截屏失败: %s", e)
            return {"ok": False, "data": {}, "error": str(e)}

    def _use_backend(self, payload: Dict[str, Any]) -> bool:
        """是否走分层注入后端：显式指定 backend 或提供 window_title（auto 目标窗口注入）。"""
        if payload.get("backend") is not None:
            return True
        return bool(payload.get("window_title"))

    def _dispatch_input(self, payload: Dict[str, Any],
                        action: str) -> Dict[str, Any]:
        """输入统一入口：backend=auto 时按 L1 PostMessage → L2 游戏桥 → L0 SendInput 分层。

        payload 支持 backend=auto|postmessage|sendinput、window_title、bridge 参数。
        sendinput 分支走注入的 *_fn（兼容测试/无显示器环境）；显式 postmessage
        强制定向目标窗口，窗口未找到返回 error。
        """
        backend = payload.get("backend", "auto")
        window_title = payload.get("window_title", "")
        bridge = payload.get("bridge")
        if backend not in ("auto", "postmessage", "sendinput"):
            return {"ok": False, "error": f"未知 backend: {backend}"}
        if backend == "auto":
            r = input_backend_mod.dispatch(window_title, action, payload, bridge=bridge)
            # postmessage/bridge 命中才直接返回；sendinput 兜底结果统一回落到注入 fn
            # （真实运行时 *_fn == input_mod 实现，行为一致；测试/无显示器走 mock）
            if r.get("ok") and r.get("data", {}).get("backend") != "sendinput":
                return r
        elif backend == "postmessage":
            hwnd = input_backend_mod._find_hwnd(window_title)
            if hwnd is None:
                return {"ok": False, "error": f"窗口未找到: {window_title}"}
            try:
                ok = input_backend_mod._dispatch_post(hwnd, action, payload)
                return {"ok": ok, "data": {"backend": "postmessage"},
                        "error": None if ok else f"PostMessage 失败: {action}"}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        # sendinput / auto（无窗口）回落：走注入 fn
        try:
            ok, data = self._dispatch_sendinput_fn(action, payload)
            return {"ok": ok, "data": dict(data, backend="sendinput"),
                    "error": None if ok else f"sendinput 失败: {action}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _dispatch_sendinput_fn(self, action: str,
                               payload: Dict[str, Any]):
        """SendInput 注入点分发（走 self.*_fn，兼容测试注入与无显示器环境）。"""
        if action == "click":
            ok = self.click_fn(int(payload.get("x", 0)), int(payload.get("y", 0)),
                               payload.get("button", "left"))
            return bool(ok), {"done": True}
        if action == "double_click":
            ok = self.double_click_fn(int(payload.get("x", 0)), int(payload.get("y", 0)),
                                      payload.get("button", "left"))
            return bool(ok), {"done": True}
        if action == "keypress":
            ok = self.keypress_fn(payload.get("key", ""),
                                  int(payload.get("repeat", 1)))
            return bool(ok), {"done": bool(ok)}
        if action == "move":
            ok = self.move_fn(int(payload.get("x", 0)), int(payload.get("y", 0)),
                              float(payload.get("duration", 0.2)))
            return bool(ok), {"done": True, "x": int(payload.get("x", 0)),
                              "y": int(payload.get("y", 0))}
        if action == "scroll":
            ok = self.scroll_fn(int(payload.get("amount", 1)),
                                payload.get("x"), payload.get("y"))
            return bool(ok), {"done": True}
        if action == "drag":
            ok = self.drag_fn(int(payload.get("x1", 0)), int(payload.get("y1", 0)),
                              int(payload.get("x2", 0)), int(payload.get("y2", 0)),
                              float(payload.get("duration", 0.5)))
            return bool(ok), {"done": True}
        return False, {"error": f"未知动作: {action}"}

    def _click(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        x = payload.get("x")
        y = payload.get("y")
        if x is None or y is None:
            return {"ok": False, "data": {}, "error": "x/y 必填"}
        if self._use_backend(payload):
            r = self._dispatch_input(payload, "click")
            if r.get("ok"):
                self._broadcast_cursor("click", x, y, payload.get("label", "点击"))
                return r
            if payload.get("backend", "auto") != "auto":
                return r
        try:
            self.click_fn(int(x), int(y), payload.get("button", "left"))
            self._broadcast_cursor("click", x, y, payload.get("label", "点击"))
            return {"ok": True, "data": {"done": True}, "error": None}
        except Exception as e:
            return {"ok": False, "data": {}, "error": str(e)}

    def _keypress(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        key = payload.get("key")
        if not key:
            return {"ok": False, "data": {}, "error": "key 必填"}
        if self._use_backend(payload):
            r = self._dispatch_input(payload, "keypress")
            if r.get("ok"):
                self._broadcast_cursor("keypress", label=f"按键 {key}")
                return r
            if payload.get("backend", "auto") != "auto":
                return r
        try:
            ok = self.keypress_fn(key, int(payload.get("repeat", 1)))
            if ok:
                self._broadcast_cursor("keypress", label=f"按键 {key}")
            return {"ok": ok, "data": {"done": ok}, "error": None if ok else f"未知按键: {key}"}
        except Exception as e:
            return {"ok": False, "data": {}, "error": str(e)}

    def _move(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """真实鼠标平滑移动（游戏操作定位用）。"""
        x = payload.get("x")
        y = payload.get("y")
        if x is None or y is None:
            return {"ok": False, "data": {}, "error": "x/y 必填"}
        if self._use_backend(payload):
            r = self._dispatch_input(payload, "move")
            if r.get("ok"):
                self._broadcast_cursor("move", x, y, payload.get("label", "移动"))
                return r
            if payload.get("backend", "auto") != "auto":
                return r
        try:
            ok = self.move_fn(int(x), int(y), float(payload.get("duration", 0.2)))
            self._broadcast_cursor("move", x, y, payload.get("label", "移动"))
            return {"ok": ok, "data": {"done": True, "x": int(x), "y": int(y)},
                    "error": None}
        except Exception as e:
            return {"ok": False, "data": {}, "error": str(e)}

    def _scroll(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """滚轮滚动。"""
        amount = payload.get("amount")
        if amount is None:
            return {"ok": False, "data": {}, "error": "amount 必填"}
        if self._use_backend(payload):
            r = self._dispatch_input(payload, "scroll")
            if r.get("ok"):
                return r
            if payload.get("backend", "auto") != "auto":
                return r
        try:
            ok = self.scroll_fn(int(amount), payload.get("x"), payload.get("y"))
            return {"ok": ok, "data": {"done": True}, "error": None}
        except Exception as e:
            return {"ok": False, "data": {}, "error": str(e)}

    def _drag(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """拖拽。"""
        if payload.get("x1") is None or payload.get("y1") is None or \
                payload.get("x2") is None or payload.get("y2") is None:
            return {"ok": False, "data": {}, "error": "x1/y1/x2/y2 必填"}
        if self._use_backend(payload):
            r = self._dispatch_input(payload, "drag")
            if r.get("ok"):
                self._broadcast_cursor("drag", payload["x2"], payload["y2"],
                                       payload.get("label", "拖拽"))
                return r
            if payload.get("backend", "auto") != "auto":
                return r
        try:
            ok = self.drag_fn(int(payload["x1"]), int(payload["y1"]),
                              int(payload["x2"]), int(payload["y2"]),
                              float(payload.get("duration", 0.5)))
            self._broadcast_cursor("drag", payload["x2"], payload["y2"],
                                   payload.get("label", "拖拽"))
            return {"ok": ok, "data": {"done": True}, "error": None}
        except Exception as e:
            return {"ok": False, "data": {}, "error": str(e)}

    def _template_match(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """模板匹配识别：在截图中定位模板。"""
        screenshot = payload.get("screenshot")
        template = payload.get("template")
        if not screenshot or not template:
            return {"ok": False, "data": {}, "error": "screenshot/template 必填"}
        try:
            match = self.template_match_fn(
                screenshot, template, float(payload.get("threshold", 0.8)))
            if match is None:
                return {"ok": True, "data": {"found": False}, "error": None}
            return {"ok": True,
                    "data": {"found": True, "x": match.x, "y": match.y,
                             "confidence": match.confidence},
                    "error": None}
        except Exception as e:
            return {"ok": False, "data": {}, "error": str(e)}

    def _cursor(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """虚拟光标控制：action ∈ move/click/double_click/right_click/show/hide/set_active_role。"""
        action = payload.get("action", "")
        if action in ("show", "hide"):
            self._event_bus.publish(SCREEN_CURSOR_VISIBILITY,
                                    role=payload.get("role", "yuki"),
                                    visible=(action == "show"))
            return {"ok": True, "data": {"done": True}, "error": None}
        if action == "set_active_role":
            self._event_bus.publish(SCREEN_CURSOR_ACTIVE_ROLE,
                                    active_role=payload.get("role", "yuki"),
                                    previous_role=self._virtual_cursor.get_active_role())
            return {"ok": True, "data": {"done": True}, "error": None}
        if action in ("move", "click", "double_click", "right_click"):
            self._broadcast_cursor(action, payload.get("x"), payload.get("y"),
                                   payload.get("label", ""))
            return {"ok": True, "data": {"done": True}, "error": None}
        return {"ok": False, "data": {}, "error": f"未知光标动作: {action}"}

    def _cursor_state(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        role = payload.get("role")
        if role:
            state = self._virtual_cursor.get_cursor_state(role)
            return {"ok": True, "data": {"cursor": state}, "error": None}
        return {"ok": True, "data": {"status": self._virtual_cursor.get_status()},
                "error": None}

    def _broadcast_cursor(self, action: str, x=None, y=None, label: str = "") -> None:
        """输入执行后广播光标动作（虚拟光标管理器订阅渲染）。"""
        try:
            self._event_bus.publish(SCREEN_CURSOR_ACTION, action=action,
                                    x=x, y=y, label=label)
        except Exception as e:
            logger.debug("[ScreenControl] 光标动作广播失败: %s", e)

    def get_cursor_state(self, role: Optional[str] = None):
        """供前端/状态提供方查询虚拟光标状态。"""
        return self._virtual_cursor.get_cursor_state(role)

    def get_cursor_status(self) -> Dict[str, Any]:
        return self._virtual_cursor.get_status()

    def _execute_plan(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """按操作计划序列执行。plan: [{"action": "click", "x":.., "y":..},
        {"action": "keypress", "key":..}, {"action": "capture"}, {"action": "wait", "seconds":..}]"""
        plan: List[Dict[str, Any]] = payload.get("plan", [])
        if not plan:
            return {"ok": False, "data": {}, "error": "plan 必填"}
        results: List[Dict[str, Any]] = []
        for step in plan:
            action = step.get("action", "")
            if action == "wait":
                import time
                time.sleep(float(step.get("seconds", 1)))
                results.append({"action": action, "done": True})
            elif action in ("click", "keypress", "capture", "move", "scroll",
                            "drag", "double_click", "template_match"):
                r = self._dispatch_sync(f"screen:{action}", step)
                results.append({"action": action, "ok": r["ok"],
                                "data": r["data"], "error": r["error"]})
                if not r["ok"]:
                    break
            else:
                results.append({"action": action, "ok": False, "error": f"未知动作: {action}"})
                break
        return {"ok": True, "data": {"results": results}, "error": None}

    def _dispatch_sync(self, capability: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """execute_plan 内部同步分发（不走 async handle）。"""
        if capability == "screen:capture":
            return self._capture(payload)
        if capability == "screen:click":
            return self._click(payload)
        if capability == "screen:keypress":
            return self._keypress(payload)
        if capability == "screen:move":
            return self._move(payload)
        if capability == "screen:scroll":
            return self._scroll(payload)
        if capability == "screen:drag":
            return self._drag(payload)
        if capability == "screen:double_click":
            return self._double_click(payload)
        if capability == "screen:template_match":
            return self._template_match(payload)
        return {"ok": False, "data": {}, "error": f"unknown capability: {capability}"}

    def _double_click(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """双击（绝对坐标）。"""
        x = payload.get("x")
        y = payload.get("y")
        if x is None or y is None:
            return {"ok": False, "data": {}, "error": "x/y 必填"}
        if self._use_backend(payload):
            r = self._dispatch_input(payload, "double_click")
            if r.get("ok"):
                self._broadcast_cursor("double_click", x, y, payload.get("label", "双击"))
                return r
            if payload.get("backend", "auto") != "auto":
                return r
        try:
            ok = self.double_click_fn(int(x), int(y), payload.get("button", "left"))
            self._broadcast_cursor("double_click", x, y, payload.get("label", "双击"))
            return {"ok": ok, "data": {"done": True}, "error": None}
        except Exception as e:
            return {"ok": False, "data": {}, "error": str(e)}
