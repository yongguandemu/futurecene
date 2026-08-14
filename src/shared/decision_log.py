"""decision_log.py — 决策日志（共享层）

把「决策」显式化为可审计记录，核心是区分三类结局：
- executed  执行了动作（L1 域内自决 / L3 总脑编排执行）
- blocked   被硬规则拦截（L0）
- no_action 系统收到但决定不动作，reason_code 必须说明「为何不回应」
- deferred  决策已作出但排队等待（如发言互斥）
- escalated 上抛到仲裁/总脑
- failed    决策过程本身失败

设计意图：调试时用「有没有日志条目」区分两种情况——
「系统没收到」不会产生任何条目；「系统决定不回应」会产生一条
outcome=no_action 且 reason_code 明确的条目。仲裁返回 None、
规则判不了静默等行为由此从隐含变为显式（规格书 5.6.4）。

事件：decision:logged（携带完整条目字段，供前端总控台/WS 消费）。

# 模块内容清单 — decision_log

## 1. 模块身份标识
- 所属调度官：shared（指挥官与全部调度官共用）
- 能力名：decision:log（决策日志 + 事件）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| max_entries | 否 | 500 | int，>=1 | 环形缓冲上限（超限淘汰最旧） |
| event_bus | 否 | None | EventBus | 注入后每次记录发布 decision:logged |

## 3. 输入契约
- 输入格式：`record(source, outcome, reason_code, layer, capability, detail, decision_id, min_interval)`
- source：必填，str，决策点标识（arbitrator/learn_brain/danmaku_pipeline/command_router）
- outcome：必填，str ∈ {executed, blocked, no_action, deferred, escalated, failed}
- reason_code：必填，str，机器可读原因（no_action 时即「为何不回应」）
- min_interval：float，同一 (source, reason_code) 的抑制间隔（周期心跳类日志防刷屏）

## 4. 输出契约
- 成功：`record()` 返回 DecisionEntry；`recent(n)` 返回最新 n 条；`stats()` 返回摘要 dict；`clear()` 清空
- 失败：抑制间隔内重复记录返回 None（不落条目、不发布事件）
- 事件：发布 `decision:logged`（ts/source/outcome/reason_code/layer/capability/detail/decision_id）

## 5. 依赖声明
- 外部服务：无
- 内部模块：`src/shared/events.DECISION_LOGGED`、event_bus（可选注入）
- 预先配置：app 装配时 attach(event_bus) 一次

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 事件发布失败 | event_bus 异常 | 记录警告，不阻断日志写入 |
| 抑制命中 | 同 (source, reason_code) 在 min_interval 内 | 返回 None，静默跳过 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| attach | 是 | 注入 EventBus（装配时调用一次） |
| clear | 是 | 清空环形缓冲（测试隔离用） |

## 8. 领域状态说明
- 状态项：`_entries`（环形缓冲）、`_last_ts`（(source, reason_code) → 最近记录时间）
- 持久化：无（内存日志，随进程生命周期）
- 恢复：无状态持久化需求；recent()/stats() 即查即用
"""
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

from src.shared.events import DECISION_LOGGED

logger = logging.getLogger(__name__)

# 决策结局（outcome 枚举值）
OUTCOME_EXECUTED = "executed"       # 执行了动作
OUTCOME_BLOCKED = "blocked"         # 被硬规则拦截（L0）
OUTCOME_NO_ACTION = "no_action"     # 收到但决定不动作（必须带 reason_code）
OUTCOME_DEFERRED = "deferred"       # 决策已作出但排队等待
OUTCOME_ESCALATED = "escalated"     # 上抛到仲裁/总脑
OUTCOME_FAILED = "failed"           # 决策过程失败


@dataclass
class DecisionEntry:
    """一条决策日志。outcome=no_action 时 reason_code 即「为何不回应」。"""

    ts: float
    source: str                 # 决策点：arbitrator / learn_brain / danmaku_pipeline / command_router
    outcome: str                # executed / blocked / no_action / deferred / escalated / failed
    reason_code: str            # 机器可读原因（no_action 必填）
    layer: str = ""             # L0 / L1 / L2 / L3
    capability: str = ""        # 相关能力名
    detail: str = ""            # 人类可读补充
    decision_id: str = ""       # 关联 ID（如仲裁 request_id）

    def to_dict(self) -> dict:
        return {"ts": self.ts, "source": self.source, "outcome": self.outcome,
                "reason_code": self.reason_code, "layer": self.layer,
                "capability": self.capability, "detail": self.detail,
                "decision_id": self.decision_id}


class DecisionLog:
    """决策日志：环形缓冲 + 事件发布 + 周期抑制。"""

    def __init__(self, max_entries: int = 500, event_bus=None):
        self._entries: Deque[DecisionEntry] = deque(maxlen=max_entries)
        self._last_ts: Dict[tuple, float] = {}
        self._bus = event_bus
        self._lock = threading.Lock()

    def attach(self, event_bus) -> None:
        self._bus = event_bus

    def record(self, source: str, outcome: str, reason_code: str,
               layer: str = "", capability: str = "", detail: str = "",
               decision_id: str = "", min_interval: float = 0.0) -> Optional[DecisionEntry]:
        """写入一条决策日志；周期抑制内重复记录返回 None。"""
        key = (source, reason_code)
        if min_interval > 0:
            now = time.time()
            with self._lock:
                last = self._last_ts.get(key, 0.0)
                if now - last < min_interval:
                    return None
                self._last_ts[key] = now
        entry = DecisionEntry(ts=time.time(), source=source, outcome=outcome,
                              reason_code=reason_code, layer=layer,
                              capability=capability, detail=detail,
                              decision_id=decision_id)
        with self._lock:
            self._entries.append(entry)
        if self._bus is not None:
            try:
                self._bus.publish(DECISION_LOGGED, **entry.to_dict())
            except Exception as e:
                logger.warning("[DecisionLog] 发布 decision:logged 失败: %s", e)
        return entry

    def recent(self, n: int = 50) -> List[DecisionEntry]:
        with self._lock:
            return list(self._entries)[-n:][::-1]   # 最新在前

    def stats(self) -> dict:
        with self._lock:
            by_outcome: Dict[str, int] = {}
            by_reason: Dict[str, int] = {}
            for e in self._entries:
                by_outcome[e.outcome] = by_outcome.get(e.outcome, 0) + 1
                by_reason[e.reason_code] = by_reason.get(e.reason_code, 0) + 1
            return {"total": len(self._entries), "by_outcome": by_outcome,
                    "by_reason_code": by_reason}

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._last_ts.clear()


# ---------- 模块级默认实例（app 装配时 attach(event_bus) 一次） ----------
default_log = DecisionLog()


def attach(event_bus) -> None:
    """装配入口：将默认决策日志接到事件总线（app.py 调用一次）。"""
    default_log.attach(event_bus)


def record_decision(source: str, outcome: str, reason_code: str,
                    layer: str = "", capability: str = "", detail: str = "",
                    decision_id: str = "", min_interval: float = 0.0) -> Optional[DecisionEntry]:
    """便捷入口：写入默认决策日志。"""
    return default_log.record(source, outcome, reason_code, layer, capability,
                              detail, decision_id, min_interval)


def recent_entries(n: int = 50) -> List[DecisionEntry]:
    return default_log.recent(n)


def log_stats() -> dict:
    return default_log.stats()


def clear_log() -> None:
    default_log.clear()
