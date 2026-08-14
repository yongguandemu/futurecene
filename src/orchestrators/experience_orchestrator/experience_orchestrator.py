"""experience_orchestrator.py — 游戏经验学习调度官主类（规格书 P2）

能力：experience:start / stop / decide / feedback / inject_task / stats /
      knowledge / plan。
职责边界：LLM 探索文本生成不直接调 llm 调度官，由接线注入 llm_fn 或 brain；
游戏动作下发经 adapter（屏幕控制 / MC 桥），不直接 import 屏幕控制调度官。

# 模块内容清单（8 项契约）
1. 模块身份标识：experience 调度官 · experience_orchestrator · 能力 experience:start/stop/decide/feedback/inject_task/stats/knowledge/plan
2. 配置契约：config 域（game 等）；brain 配置经 payload.config 合并
3. 输入契约：handle(command) — capability + payload（state/scene/adapter/goal/config 等）
4. 输出契约：返回 {"ok","data","error"}；发布 EXPERIENCE_RECORDED（经 brain）
5. 依赖声明：logging/typing；registry、ExperienceLearnBrain、game_registry；src.shared.events
6. 错误定义：brain 未启动 → {"ok": False, "error": "brain 未启动"}；未知 capability 返回错误
7. 生命周期方法：start(adapter)/stop()/health()/handle()；brain 在 start 时创建
8. 领域状态说明：_started、_brain（ExperienceLearnBrain）、_adapter、_event_bus、_config
"""
import logging
from typing import Any, Dict, List, Optional

from src.orchestrators.experience_orchestrator import registry
from src.orchestrators.experience_orchestrator.learn_brain import ExperienceLearnBrain
from src.orchestrators.experience_orchestrator import game_registry
from src.shared.events import EXPERIENCE_RECORDED

logger = logging.getLogger(__name__)


class ExperienceOrchestrator:
    """游戏经验学习调度官。"""

    name = "experience"

    def __init__(self, event_bus, config: Optional[Dict[str, Any]] = None,
                 adapter=None, brain: Optional[ExperienceLearnBrain] = None):
        self._event_bus = event_bus
        self._config = config or {}
        self._adapter = adapter
        self._brain = brain  # 测试注入；start 时创建
        self._started = False
        registry.bind(self.handle)

    def capabilities(self) -> List[str]:
        return registry.capabilities()

    def start(self, adapter=None) -> None:
        if self._brain is None:
            self._adapter = adapter or self._adapter
            self._brain = ExperienceLearnBrain(self._adapter, self._config,
                                               event_bus=self._event_bus)
        self._brain.set_bus(self._event_bus)
        self._brain.start()
        self._started = True
        logger.info("[ExperienceOrchestrator] 已启动 (game=%s)",
                    self._config.get("game", ""))

    async def handle(self, command: Dict[str, Any]) -> Dict[str, Any]:
        capability = command.get("capability", "")
        payload = command.get("payload", {})
        if capability == "experience:start":
            return self._start(payload)
        if capability == "experience:stop":
            return self._stop()
        if capability == "experience:decide":
            return self._decide(payload)
        if capability == "experience:feedback":
            return self._feedback(payload)
        if capability == "experience:inject_task":
            return self._inject_task(payload)
        if capability == "experience:stats":
            return {"ok": True, "data": self._stats(), "error": None}
        if capability == "experience:knowledge":
            return self._knowledge()
        if capability == "experience:plan":
            return self._plan(payload)
        return {"ok": False, "data": {}, "error": f"unknown capability: {capability}"}

    def health(self) -> Dict[str, Any]:
        running = bool(self._brain and self._brain._thread and
                       self._brain._thread.is_alive())
        return {"status": "ok" if self._started else "down", "detail": f"loop_running={running}"}

    def stop(self) -> None:
        if self._brain:
            self._brain.stop()
        self._started = False

    # ---------- 内部实现 ----------

    def _start(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        adapter = payload.get("adapter") or self._adapter
        if self._brain is None:
            self._adapter = adapter
            brain_cfg = dict(self._config)
            brain_cfg.update(payload.get("config") or {})
            self._brain = ExperienceLearnBrain(adapter, brain_cfg,
                                               event_bus=self._event_bus)
        self._brain.set_bus(self._event_bus)
        if not (self._brain._thread and self._brain._thread.is_alive()):
            self._brain.start()
        self._started = True
        return {"ok": True, "data": {"started": True}, "error": None}

    def _stop(self) -> Dict[str, Any]:
        if self._brain:
            self._brain.stop()
        self._started = False
        return {"ok": True, "data": {"stopped": True}, "error": None}

    def _decide(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._brain is None:
            return {"ok": False, "data": {}, "error": "brain 未启动"}
        state = payload.get("state") or {}
        scene = payload.get("scene") or {"state": state}
        self._brain._decide(_state_from_dict(state), scene)
        return {"ok": True, "data": {"decided": True}, "error": None}

    def _feedback(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._brain is None:
            return {"ok": False, "data": {}, "error": "brain 未启动"}
        self._brain.on_feedback(bool(payload.get("state_changed")),
                                bool(payload.get("event_positive")),
                                payload.get("error_context", ""))
        return {"ok": True, "data": {"feedbacked": True}, "error": None}

    def _inject_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._brain is None:
            return {"ok": False, "data": {}, "error": "brain 未启动"}
        ok = self._brain.inject_task(payload.get("goal", ""))
        return {"ok": ok, "data": {"injected": ok}, "error": None if ok else "goal 为空"}

    def _stats(self) -> Dict[str, Any]:
        if self._brain is None:
            return {"started": False, "entries": 0}
        return dict(self._brain.stats())

    def _knowledge(self) -> Dict[str, Any]:
        games = game_registry.list_games()
        return {"ok": True,
                "data": {"games": games,
                         "current": self._config.get("game", "")},
                "error": None}

    def _plan(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._brain is None or self._brain._planner is None:
            return {"ok": False, "data": {}, "error": "planner 未启用"}
        chain = self._brain._planner.plan(payload.get("goal", ""),
                                          payload.get("state") or {})
        return {"ok": True, "data": {"subtasks": chain}, "error": None}


def _state_from_dict(state: dict):
    """dict → GameState（供 decide 命令校验用）。"""
    from src.orchestrators.experience_orchestrator.state_encoder import GameState
    return GameState(
        scene_type=(state or {}).get("scene_type", "unknown"),
        text=(state or {}).get("text", ""),
        fingerprint=(state or {}).get("fingerprint", ""),
        hud=(state or {}).get("hud") or {},
        timestamp=(state or {}).get("timestamp", 0.0))