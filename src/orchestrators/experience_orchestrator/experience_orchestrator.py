"""experience_orchestrator.py — 游戏经验学习调度官主类（规格书 P2）

能力：experience:start / stop / decide / feedback / inject_task / stats /
      knowledge / plan。
职责边界：LLM 探索文本生成不直接调 llm 调度官，由接线注入 llm_fn 或 brain；
游戏动作下发经 adapter（屏幕控制 / MC 桥），不直接 import 屏幕控制调度官。

# 模块内容清单 — experience_orchestrator

## 1. 模块身份标识
- 所属调度官：experience
- 能力名：experience:start / experience:stop / experience:decide / experience:feedback / experience:inject_task / experience:stats / experience:knowledge / experience:plan

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| game | 否 | "" | str | 游戏名称（决定知识库加载） |
| 其他 | 否 | 继承 | dict | brain 配置经 payload.config 合并传递给 ExperienceLearnBrain |

## 3. 输入契约
- 输入格式：`handle(command)` — dict 含 `capability`（str）+ `payload`（dict）
- payload 字段按 capability 不同：state/scene/adapter/goal/config 等
- 外部调用：`start(adapter)` 直接启动 brain

## 4. 输出契约
- 成功：返回 `{"ok": True, "data": dict, "error": None}`
- 失败：brain 未启动返回 `{"ok": False, "data": {}, "error": "brain 未启动"}`；未知 capability 返回 `{"ok": False, "data": {}, "error": "unknown capability: ..."}`
- 事件：经 brain 发布 `EXPERIENCE_RECORDED / EXPERIENCE_QUERIED / EXPERIENCE_GOAL_COMPLETED`

## 5. 依赖声明
- 外部服务：无（游戏动作下发经 adapter，不直接依赖外部服务）
- 内部模块：`experience_orchestrator/registry`、`learn_brain.ExperienceLearnBrain`、`game_registry`、`src/shared/events`
- 预先配置：无（start 时注入 adapter）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| brain 未启动 | decide/feedback/inject_task 时 brain 为 None | 返回 error，提示先 start |
| 未知 capability | handle 收到未注册的能力名 | 返回 error，记录警告 |
| 子模块异常 | brain 内部异常 | 经 brain 异常处理，不泄露到上层 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start | 是 | 创建 ExperienceLearnBrain（若未注入），启动 brain 线程 |
| stop | 是 | 停止 brain 线程 |
| health | 是 | 返回 {"status": "ok"/"down", "detail": str} |

## 8. 领域状态说明
- 状态项：`_started`（bool）、`_brain`（ExperienceLearnBrain）、`_adapter`（游戏适配器）、`_event_bus`、`_config`
- 持久化：经验数据由 ExperienceStore 管理（本地 json 文件）
- 恢复：start 时重建 brain；stop 后回到未启动状态
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
        if self._brain is not None:
            # 测试注入的 brain：直接启用
            self._brain.set_bus(self._event_bus)
            self._brain.start()
            self._started = True
            return
        self._adapter = adapter or self._adapter
        if self._adapter is None:
            # D3 模块傻：无游戏 adapter 时看不到任何游戏状态，决策循环只会空转
            # （反复尝试 move_to 等动作全部 push_failed + 刷爆决策日志）。
            # 不自动启动，待经 experience:start(adapter) 显式拉起。
            logger.info("[ExperienceOrchestrator] 无游戏 adapter，跳过决策循环"
                        "（可经 experience:start 显式启动）")
            self._started = True
            return
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