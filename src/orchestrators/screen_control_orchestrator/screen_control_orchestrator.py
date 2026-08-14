"""screen_control_orchestrator.py — 屏幕控制调度官主类（规格书 5.4）

能力：screen:capture / screen:click / screen:keypress / screen:execute_plan。
职责边界（5.5）：不主动截屏（除游戏实况调度官内部定时外），只响应命令调用。

# 模块内容清单（8 项契约）
1. 模块身份标识：screen · ScreenControlOrchestrator · 能力 screen:capture/click/keypress/execute_plan
2. 配置契约：vision_api_key 视觉模型密钥（可选）
3. 输入契约：handle(command) 指令字典（capability + payload：region/window_title/x/y/key/plan 等）
4. 输出契约：{ok, data, error} 响应字典；execute_plan 返回逐步结果列表
5. 依赖声明：logging、typing、registry、capture、input、vision 模块
6. 错误定义：截屏/点击/按键异常捕获并返回 error；x/y/key/plan 缺失返回 error
7. 生命周期方法：start()/stop()/health()
8. 领域状态说明：_started 启动标记；_vision_api_key；capture/click/keypress/ocr/describe 函数注入点
"""
import logging
from typing import Any, Dict, List, Optional

from src.orchestrators.screen_control_orchestrator import registry
from src.orchestrators.screen_control_orchestrator import capture as capture_mod
from src.orchestrators.screen_control_orchestrator import input as input_mod
from src.orchestrators.screen_control_orchestrator import vision as vision_mod

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
        self.ocr_fn = vision_mod.ocr
        self.describe_fn = vision_mod.describe
        registry.bind(self.handle)

    def capabilities(self) -> List[str]:
        return registry.capabilities()

    def start(self) -> None:
        self._started = True
        logger.info("[ScreenControlOrchestrator] 已启动")

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
        return {"ok": False, "data": {}, "error": f"unknown capability: {capability}"}

    def health(self) -> Dict[str, Any]:
        return {"status": "ok" if self._started else "down", "detail": ""}

    def stop(self) -> None:
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

    def _click(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        x = payload.get("x")
        y = payload.get("y")
        if x is None or y is None:
            return {"ok": False, "data": {}, "error": "x/y 必填"}
        try:
            self.click_fn(int(x), int(y), payload.get("button", "left"))
            return {"ok": True, "data": {"done": True}, "error": None}
        except Exception as e:
            return {"ok": False, "data": {}, "error": str(e)}

    def _keypress(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        key = payload.get("key")
        if not key:
            return {"ok": False, "data": {}, "error": "key 必填"}
        try:
            ok = self.keypress_fn(key, int(payload.get("repeat", 1)))
            return {"ok": ok, "data": {"done": ok}, "error": None if ok else f"未知按键: {key}"}
        except Exception as e:
            return {"ok": False, "data": {}, "error": str(e)}

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
            elif action in ("click", "keypress", "capture"):
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
        return {"ok": False, "data": {}, "error": f"unknown capability: {capability}"}
