"""game_orchestrator.py — 游戏实况调度官主类（规格书 5.4）

能力：game:vn_start / vn_stop / vn_state / mc_start / mc_stop / commentary。
职责边界（5.5）：解说生成不直接调 LLM，发布 game:commentary_requested，经指挥官编排；
VN 画面轮询为本调度官内部定时行为（屏幕控制不主动截屏的例外）。

# 模块内容清单（8 项契约）
1. 模块身份标识：game 调度官 · game_orchestrator · 能力 game:vn_start/vn_stop/vn_state/mc_start/mc_stop/commentary
2. 配置契约：无（VN profile 经 payload.profile_name 指定，默认 atri）
3. 输入契约：handle(command) — capability + payload（profile_name/mode/scene_state 等）
4. 输出契约：返回 {"ok","data","error"}；发布 game:commentary_requested；VN 状态变化经 session 发布 game:vn_state_changed
5. 依赖声明：logging/typing；registry、MCBridge、VNSession/load_profile；src.shared.events
6. 错误定义：未知 capability 返回错误；VN/MC 内部异常由 session/bridge 捕获记录
7. 生命周期方法：start()/stop()/health()/handle()；session 在 vn_start 时创建
8. 领域状态说明：_started、_session（VNSession）、_bridge（MCBridge）、_screen（屏幕控制调度官引用）
"""
import logging
from typing import Any, Dict, List, Optional

from src.orchestrators.game_orchestrator import registry
from src.orchestrators.game_orchestrator.mc_bridge import MCBridge
from src.orchestrators.game_orchestrator.vn_session import VNSession, load_profile
from src.shared.events import GAME_COMMENTARY_REQUESTED

logger = logging.getLogger(__name__)


class GameOrchestrator:
    """游戏实况调度官。"""

    name = "game"

    def __init__(self, event_bus, screen_orchestrator=None,
                 session: Optional[VNSession] = None, bridge: Optional[MCBridge] = None):
        self._event_bus = event_bus
        self._screen = screen_orchestrator  # 屏幕控制调度官（命令调用，不 import）
        self._session = session  # 测试注入；vn_start 时创建
        self._bridge = bridge or MCBridge()
        self._started = False
        registry.bind(self.handle)

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
        return {"ok": False, "data": {}, "error": f"unknown capability: {capability}"}

    def health(self) -> Dict[str, Any]:
        vn_running = bool(self._session and self._session._running)
        return {"status": "ok" if self._started else "down",
                "detail": f"vn={vn_running} mc={self._bridge.running}"}

    def stop(self) -> None:
        if self._session:
            self._session.stop()
        self._started = False

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
