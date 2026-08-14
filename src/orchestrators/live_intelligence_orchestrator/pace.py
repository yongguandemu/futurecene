"""模块内容清单 — pace

## 1. 模块身份标识
- 所属调度官：live_intelligence_orchestrator
- 能力名：intel:pace_decide
- 引擎名：（单实现）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| min_commentary_interval | 否 | 20 | float 秒 | 两次解说最小间隔 |
| dialogue_quiet_seconds | 否 | 8 | float 秒 | 对白静默多少秒后允许解说 |

## 3. 输入契约
- intel:pace_decide 输入：{"state": str, "text"?: str, "now"?: float}
  - state 必填，str ∈ {dialogue,choice,menu,puzzle,transition,cg,unknown}
  - text 可选，str
  - now 可选，float，默认 time.time()

## 4. 输出契约
- 成功：{"ok": true, "data": {"should_comment": bool, "reason": str, "priority": int}, "error": null}
- 失败：{"ok": false, "data": {}, "error": str}

## 5. 依赖声明
- 外部服务：无
- 内部模块：vn_screen_state（VNScreenState）、shared/events（PACE_DECIDED，可选）
- 预先配置：无

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| ValueError | state 非法 | 调用方校验输入 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| init | 是 | 读取间隔配置 |
| start/stop | 否 | 无状态线程，无需生命周期 |
| health | 是 | 返回配置与最近解说时间 |

## 8. 领域状态说明
- 状态项：_last_comment_at、_last_dialogue_text、_last_dialogue_change_at
- 持久化：无
- 恢复：无
"""
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict

from src.orchestrators.live_intelligence_orchestrator.vn_screen_state import VNScreenState
from src.shared.events import PACE_DECIDED

logger = logging.getLogger(__name__)

_VALID_STATES = {"dialogue", "choice", "menu", "puzzle", "transition", "cg", "unknown"}


@dataclass
class VNPaceDecision:
    """解说节奏决策结果。"""
    should_comment: bool
    reason: str
    priority: int = 0


class VNPaceController:
    """控制陪看吐槽的说话时机，避免频繁打断剧情。"""

    def __init__(self, event_bus=None, min_commentary_interval: float = 20.0,
                 dialogue_quiet_seconds: float = 8.0):
        self.event_bus = event_bus
        self.min_commentary_interval = float(min_commentary_interval)
        self.dialogue_quiet_seconds = float(dialogue_quiet_seconds)
        self._last_comment_at = 0.0
        self._last_dialogue_text = None
        self._last_dialogue_change_at = 0.0

    def health(self) -> Dict[str, Any]:
        return {"status": "ok",
                "detail": f"last_comment_at={self._last_comment_at:.1f}"}

    # ---------- 核心操作 ----------

    def decide(self, state: VNScreenState, now: float = None) -> VNPaceDecision:
        now = time.time() if now is None else float(now)
        s = state.state
        if s == "choice":
            self._last_comment_at = now
            return VNPaceDecision(True, "choice_requires_commentary", 90)
        if s in ("puzzle", "menu"):
            self._last_comment_at = now
            return VNPaceDecision(True, f"{s}_requires_commentary", 80)
        if s in ("transition", "cg"):
            if self._cooldown_ready(now):
                self._last_comment_at = now
                return VNPaceDecision(True, f"{s}_reaction", 45)
            return VNPaceDecision(False, "commentary_cooldown", 0)
        if s == "dialogue":
            changed = state.text and state.text != self._last_dialogue_text
            if changed:
                self._last_dialogue_text = state.text
                self._last_dialogue_change_at = now
                if self._last_comment_at == 0.0:
                    self._last_comment_at = now
                    return VNPaceDecision(True, "first_dialogue_commentary", 35)
                return VNPaceDecision(False, "dialogue_is_changing", 0)
            quiet = now - self._last_dialogue_change_at if self._last_dialogue_change_at else 0.0
            if quiet >= self.dialogue_quiet_seconds and self._cooldown_ready(now):
                self._last_comment_at = now
                return VNPaceDecision(True, "dialogue_quiet_commentary", 35)
            return VNPaceDecision(False, "dialogue_too_dense", 0)
        if self._cooldown_ready(now):
            self._last_comment_at = now
            return VNPaceDecision(True, "unknown_state_probe", 10)
        return VNPaceDecision(False, "commentary_cooldown", 0)

    def decide_state(self, state: str, text: str = "", now: float = None) -> VNPaceDecision:
        """字符串接口，供调度官 handle 调用。"""
        state = (state or "").strip().lower()
        if state not in _VALID_STATES:
            raise ValueError(f"state must be one of {sorted(_VALID_STATES)}")
        decision = self.decide(VNScreenState(state=state, text=text), now=now)
        if self.event_bus:
            try:
                self.event_bus.publish(PACE_DECIDED, state=state,
                                       should_comment=decision.should_comment,
                                       reason=decision.reason)
            except Exception as e:
                logger.warning("[Pace] 发布事件失败: %s", e)
        return decision

    # ---------- 内部 ----------

    def _cooldown_ready(self, now: float) -> bool:
        return (now - self._last_comment_at) >= self.min_commentary_interval