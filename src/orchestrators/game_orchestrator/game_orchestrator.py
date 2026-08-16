"""game_orchestrator.py — 游戏实况调度官主类（规格书 5.4）

能力：game:vn_start / vn_stop / vn_state / mc_start / mc_stop / commentary /
game:op_start / op_stop / op_state / op_plan / op_command（通用游戏操作）。
职责边界（5.5）：解说生成不直接调 LLM，发布 game:commentary_requested，经指挥官编排；
VN 画面轮询为本调度官内部定时行为（屏幕控制不主动截屏的例外）。
通用游戏操作：感知/操作经 screen 调度官命令调用（不直接 import），
全流程循环（感知→判断→操作→反馈）由 GameOperationLoop 承载。

# 模块内容清单（8 项契约）
1. 模块身份标识：game 调度官 · game_orchestrator · 能力 game:vn_*/mc_*/commentary/op_* 共 11 项
2. 配置契约：无（VN profile 经 payload.profile_name 指定，默认 atri；操作配置经 payload 或 config）
3. 输入契约：handle(command) — capability + payload（profile_name/mode/scene_state/window_title/click_x/click_y/command/action 等）
4. 输出契约：返回 {"ok","data","error"}；发布 game:commentary_requested；VN 状态变化经 session 发布 game:vn_state_changed；操作循环发布 game:op_*
5. 依赖声明：logging/typing；registry、MCBridge、VNSession/load_profile、GameOperationLoop/Controller/Safety/Planner；src.shared.events
6. 错误定义：未知 capability 返回错误；VN/MC/操作内部异常由 session/bridge/loop 捕获记录
7. 生命周期方法：start()/stop()/health()/handle()；session 在 vn_start 时创建；操作循环在 op_start 时创建
8. 领域状态说明：_started、_session（VNSession）、_bridge（MCBridge）、_screen（屏幕控制调度官引用）、_op_loop（操作循环）
"""
import asyncio
import logging
import threading
from typing import Any, Dict, List, Optional

from src.orchestrators.game_orchestrator import registry
from src.orchestrators.game_orchestrator.game_operation_controller import (
    GameOperationController,
    OperationSafety,
)
from src.orchestrators.game_orchestrator.game_operation_loop import GameOperationLoop
from src.orchestrators.game_orchestrator.game_operation_planner import (
    GameOperationPlanner,
)
from src.orchestrators.game_orchestrator.mc_bridge import MCBridge
from src.orchestrators.game_orchestrator.vn_session import VNSession, load_profile
from src.shared.events import GAME_COMMENTARY_REQUESTED

logger = logging.getLogger(__name__)


class GameOrchestrator:
    """游戏实况调度官。"""

    name = "game"

    def __init__(self, event_bus, screen_orchestrator=None,
                 session: Optional[VNSession] = None, bridge: Optional[MCBridge] = None,
                 config: Optional[Dict[str, Any]] = None):
        self._event_bus = event_bus
        self._screen = screen_orchestrator  # 屏幕控制调度官（命令调用，不 import）
        self._session = session  # 测试注入；vn_start 时创建
        self._bridge = bridge or MCBridge()
        self._config = config or {}
        self._started = False
        # 专用事件循环线程：桥接 async screen.handle 与同步操作循环/命令上下文
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_loop, daemon=True,
                                             name="game-orch-loop")
        self._loop_thread.start()
        # 通用游戏操作：使能状态机 + 安全护栏 + 规划器 + 全流程循环
        op_cfg = self._config.get("operation", {}) or {}
        self._op_controller = GameOperationController(op_cfg)
        self._op_safety = OperationSafety(op_cfg)
        self._op_planner = GameOperationPlanner(chat_fn=self._op_chat)
        self._llm_orchestrator = None   # 经 set_llm_orchestrator 注入（指挥官 llm:chat 编排）
        self._experience_orchestrator = None  # 经 set_experience_orchestrator 注入
        self._op_loop = GameOperationLoop(
            controller=self._op_controller,
            safety=self._op_safety,
            perceive_fn=self._op_perceive,
            act_fn=self._op_act,
            event_bus=event_bus,
            planner=self._op_planner,
            experience_fn=self._op_experience,
            commentary_fn=self._op_commentary,
            config=op_cfg,
        )
        self._op_window_title = ""
        self._op_click_x = 960
        self._op_click_y = 940
        registry.bind(self.handle)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def capabilities(self) -> List[str]:
        return registry.capabilities()

    def start(self) -> None:
        self._started = True
        logger.info("[GameOrchestrator] 已启动")

    async def handle(self, command: Dict[str, Any]) -> Dict[str, Any]:
        capability = command.get("capability", "")
        payload = command.get("payload", {})
        if capability == "game:vn_start":
            return self._vn_start(payload)
        if capability == "game:vn_stop":
            return self._vn_stop()
        if capability == "game:vn_state":
            return self._vn_state()
        if capability == "game:mc_start":
            return self._mc_start(payload)
        if capability == "game:mc_stop":
            return self._mc_stop()
        if capability == "game:commentary":
            return self._commentary(payload)
        if capability == "game:op_start":
            return self._op_start(payload)
        if capability == "game:op_stop":
            return self._op_stop()
        if capability == "game:op_state":
            return self._op_state()
        if capability == "game:op_plan":
            return self._op_plan(payload)
        if capability == "game:op_command":
            return self._op_command(payload)
        return {"ok": False, "data": {}, "error": f"unknown capability: {capability}"}

    def health(self) -> Dict[str, Any]:
        vn_running = bool(self._session and self._session._running)
        op_running = bool(self._op_loop and self._op_loop.snapshot().get("running"))
        return {"status": "ok" if self._started else "down",
                "detail": f"vn={vn_running} mc={self._bridge.running} op={op_running}"}

    def stop(self) -> None:
        if self._session:
            self._session.stop()
        self._op_loop.stop()
        self._started = False
        # 关闭专用事件循环线程
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop_thread.join(timeout=3)
        except Exception as e:
            logger.debug("[GameOrchestrator] 事件循环关闭失败: %s", e)

    # ---------- 内部实现 ----------

    def _vn_start(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        profile_name = payload.get("profile_name", "atri")
        if self._session is None:
            profile = load_profile(profile_name)
            self._session = VNSession(profile=profile, screen_orchestrator=self._screen,
                                      event_bus=self._event_bus)
        self._session.start()
        return {"ok": True, "data": {"started": True,
                                     "profile": self._session.profile.name}, "error": None}

    def _vn_stop(self) -> Dict[str, Any]:
        if self._session:
            self._session.stop()
        return {"ok": True, "data": {"stopped": True}, "error": None}

    def _vn_state(self) -> Dict[str, Any]:
        if not self._session:
            return {"ok": True, "data": {"state": "not_started"}, "error": None}
        return {"ok": True, "data": self._session.snapshot(), "error": None}

    def _mc_start(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self._bridge.start(mode=payload.get("mode", "live"))
        return {"ok": result["started"], "data": result, "error": None}

    def _mc_stop(self) -> Dict[str, Any]:
        result = self._bridge.stop()
        return {"ok": True, "data": result, "error": None}

    def _commentary(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """生成解说：发布 game:commentary_requested，回复文本由指挥官编排（5.5 职责边界）。"""
        scene_state = payload.get("scene_state", "")
        self._event_bus.publish(GAME_COMMENTARY_REQUESTED, scene_state=scene_state)
        return {"ok": True,
                "data": {"commentary_text": "[解说生成中，经指挥官编排后返回]"},
                "error": None}

    # ---------- 通用游戏操作 ----------

    def _op_start(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """开启 AI 自动操作循环。payload: window_title/click_x/click_y/source/stop_after_seconds。"""
        self._op_window_title = payload.get("window_title", self._op_window_title)
        self._op_click_x = int(payload.get("click_x", self._op_click_x))
        self._op_click_y = int(payload.get("click_y", self._op_click_y))
        source = payload.get("source", "manual")
        stop_after = payload.get("stop_after_seconds")
        self._op_controller.start(source=source,
                                  stop_after_seconds=stop_after)
        self._op_loop.start()
        return {"ok": True, "data": self._op_controller.status(), "error": None}

    def _op_stop(self) -> Dict[str, Any]:
        """关闭 AI 自动操作循环（用户接管）。"""
        self._op_loop.stop()
        result = self._op_controller.stop()
        return {"ok": True, "data": result, "error": None}

    def _op_state(self) -> Dict[str, Any]:
        """操作循环状态快照。"""
        return {"ok": True, "data": self._op_loop.snapshot(), "error": None}

    def _op_plan(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """自然语言指令 → 操作计划（不执行）。"""
        command = payload.get("command", "")
        if not command:
            return {"ok": False, "data": {}, "error": "command 必填"}
        plan = self._op_planner.generate_plan(command)
        return {"ok": True, "data": {"command": command, "plan": plan}, "error": None}

    def _op_command(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """单条指令执行：command（自然语言）或 action+params（结构化）。"""
        if not self._op_controller.enabled:
            return {"ok": False, "data": {}, "error": "AI 自动操作未开启（先调 game:op_start）"}
        if payload.get("action"):
            action = payload["action"]
            params = payload.get("params", {})
        else:
            command = payload.get("command", "")
            plan = self._op_planner.generate_plan(command)
            if not plan:
                return {"ok": False, "data": {}, "error": f"无法理解指令: {command}"}
            action = plan[0]["action"]
            params = plan[0].get("params", {})
        result = self._op_act(action, params)
        return {"ok": result.get("ok", False), "data": result, "error": None}

    # ---------- 操作循环回调（感知/操作/LLM） ----------

    def _op_perceive(self) -> Dict[str, Any]:
        """感知：截屏 + OCR → 场景（text/state/options/image_path）。"""
        if self._screen is None:
            return {"text": "", "state": "unknown", "options": [], "image_path": ""}
        result = self._call_screen("screen:capture",
                                   {"window_title": self._op_window_title,
                                    "with_ocr": True})
        data = result.get("data", {})
        text = (data.get("text") or "").strip()
        state = self._classify_state(text)
        return {"text": text, "state": state, "options": [],
                "image_path": data.get("image_path", "")}

    def _op_act(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """操作：经 screen 调度官命令调用执行，并广播虚拟光标。

        P1 注入修复：透传 window_title + backend=auto，使输入经 input_backend
        分层注入（L1 PostMessage 后台窗口 → L2 游戏桥 → L0 SendInput 前台兜底），
        解决 SendInput 只投递前台窗口导致的注入阻塞。
        """
        if self._screen is None:
            return {"ok": False, "scene_changed": False, "error": "screen not bound"}
        base = {"window_title": self._op_window_title, "backend": "auto"}
        if action == "advance":
            r = self._call_screen("screen:click",
                                  dict(base, x=self._op_click_x, y=self._op_click_y,
                                       label="推进"))
        elif action == "select_option":
            r = self._call_screen("screen:keypress", dict(base, key="DOWN"))
            if r.get("ok"):
                r = self._call_screen("screen:keypress", dict(base, key="ENTER"))
        elif action == "keypress":
            r = self._call_screen("screen:keypress",
                                  dict(base, key=params.get("key", ""), label="按键"))
        elif action == "hold":
            r = self._call_screen("screen:keypress",
                                  dict(base, key=params.get("key", ""), label="按住"))
        elif action == "release":
            r = self._call_screen("screen:keypress",
                                  dict(base, key=params.get("key", ""), label="松开"))
        elif action == "click":
            r = self._call_screen("screen:click",
                                  dict(base, x=params.get("x"), y=params.get("y"),
                                       button=params.get("button", "left"),
                                       label="点击"))
        elif action == "double_click":
            r = self._call_screen("screen:double_click",
                                  dict(base, x=params.get("x"), y=params.get("y"),
                                       button=params.get("button", "left"),
                                       label="双击"))
        elif action == "move":
            r = self._call_screen("screen:move",
                                  dict(base, x=params.get("x"), y=params.get("y"),
                                       duration=params.get("duration", 0.2),
                                       label="移动"))
        elif action == "drag":
            r = self._call_screen("screen:drag",
                                  dict(base, x1=params.get("x1"), y1=params.get("y1"),
                                       x2=params.get("x2"), y2=params.get("y2"),
                                       duration=params.get("duration", 0.5),
                                       label="拖拽"))
        elif action == "scroll":
            r = self._call_screen("screen:scroll",
                                  dict(base, amount=params.get("amount", 1),
                                       x=params.get("x"), y=params.get("y")))
        elif action == "type":
            r = self._call_screen("screen:keypress",
                                  dict(base, key=(params.get("text") or "")[:1],
                                       label="输入"))
        elif action == "capture":
            r = self._call_screen("screen:capture", {})
        elif action == "wait":
            import time
            time.sleep(float(params.get("seconds", 1)))
            r = {"ok": True, "data": {"done": True}, "error": None}
        else:
            r = {"ok": False, "data": {}, "error": f"未知操作: {action}"}
        return {"ok": bool(r.get("ok", False)),
                "scene_changed": True, "error": r.get("error")}

    def _op_chat(self, prompt: str) -> str:
        """LLM 规划回调：经指挥官 llm:chat 编排（pro 引擎，DeepSeek V4 Pro 优先——JSON 计划生成需强推理；
        未注入 llm 调度官时返回空，走模板路径）。"""
        if self._llm_orchestrator is None:
            return ""
        r = self._call_orchestrator(self._llm_orchestrator, "llm:chat",
                                    {"text": prompt, "engine": "pro"})
        if r.get("ok"):
            return r.get("data", {}).get("reply", "") or ""
        logger.warning("[GameOrchestrator] LLM 规划失败: %s", r.get("error"))
        return ""

    def _op_experience(self, action: str, params: Dict[str, Any],
                       scene: Dict[str, Any]) -> None:
        """操作成功且场景变化 → 记录正向经验（经 experience:feedback 联动）。"""
        if self._experience_orchestrator is None:
            return
        self._call_orchestrator(self._experience_orchestrator,
                                "experience:feedback",
                                {"state_changed": True, "event_positive": True,
                                 "error_context": ""})

    def _op_commentary(self, action: str, scene: Dict[str, Any]) -> None:
        """操作后请求解说（经 game:commentary_requested 联动指挥官编排）。"""
        scene_state = (scene.get("text") or "").strip()[:40] or f"操作完成: {action}"
        self._event_bus.publish(GAME_COMMENTARY_REQUESTED,
                                scene_state=scene_state)

    def set_llm_orchestrator(self, llm_orchestrator) -> None:
        """注入 LLM 调度官引用（装配层调用，游戏操作规划 LLM 路径）。"""
        self._llm_orchestrator = llm_orchestrator

    def set_experience_orchestrator(self, experience_orchestrator) -> None:
        """注入经验学习调度官引用（装配层调用，操作反馈经验联动）。"""
        self._experience_orchestrator = experience_orchestrator

    def _classify_state(self, text: str) -> str:
        """基于 OCR 文本粗分类画面状态（对白/菜单/unknown）。"""
        if not text:
            return "unknown"
        low = text.lower()
        if any(k in low for k in ("new game", "continue", "load", "设置", "菜单")):
            return "menu"
        return "dialogue"

    def _call_screen(self, capability: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """跨域命令调用 screen 调度官（经专用事件循环线程，线程安全）。"""
        return self._call_orchestrator(self._screen, capability, payload)

    def _call_orchestrator(self, orch, capability: str,
                           payload: Dict[str, Any]) -> Dict[str, Any]:
        """跨域命令调用任意调度官（经专用事件循环线程，线程安全）。"""
        if orch is None:
            return {"ok": False, "data": {}, "error": "orchestrator not bound"}
        try:
            future = asyncio.run_coroutine_threadsafe(
                orch.handle({"capability": capability, "payload": payload}),
                self._loop)
            return future.result(timeout=30)
        except Exception as e:
            logger.error("[GameOrchestrator] %s 调用失败 %s: %s",
                         getattr(orch, "name", "?"), capability, e)
            return {"ok": False, "data": {}, "error": str(e)}
