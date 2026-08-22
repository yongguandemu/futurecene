"""context_aggregator.py — 上下文聚合（总控调度化，规格 2026-08-22 任务一）

合并短期记忆 + reference 资料（世界书/脚本等）+ 会话状态 → 上下文快照。
供 LLM 调用方与批量问询（任务二）使用；reference 槽位由调用方填充（不排队）。

# 模块内容清单 — context_aggregator

## 1. 模块身份标识
- 所属调度官：commander（input 域）
- 能力名：无（指挥官内部服务）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| 无 | - | - | - | 依赖注入 memory/session/event_bus |

## 3. 输入契约
- build(role="", reference=None, max_history=20) -> dict

## 4. 输出契约
- 成功：{"history", "session", "reference", "snapshot_ts"}
- 失败：无异常路径（缺失依赖返回空段）

## 5. 依赖声明
- 外部服务：无
- 内部模块：memory_orchestrator（可选）、session_context（可选）、shared.events（可选）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 无 | - | 依赖缺失时对应段为空 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | - | 无状态 |

## 8. 领域状态说明
- 状态项：无
- 持久化：无
"""
import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ContextAggregator:
    """短期记忆 + reference + 会话状态 → 上下文快照。"""

    def __init__(self, memory=None, session=None, event_bus=None):
        self._memory = memory
        self._session = session
        self._event_bus = event_bus

    async def build(self, role: str = "", reference: Optional[List[Dict]] = None,
                    max_history: int = 20) -> Dict[str, Any]:
        history: List[Dict[str, str]] = []
        if self._memory is not None:
            try:
                payload = {"session_id": "default", "limit": max_history}
                if role:
                    payload["character_id"] = role
                result = await self._memory.handle({
                    "capability": "memory:get_history", "payload": payload})
                history = result.get("data", {}).get("history", []) or []
            except Exception as e:
                logger.warning("[ContextAggregator] 记忆读取失败: %s", e)
        session_snap = {}
        if self._session is not None:
            try:
                session_snap = self._session.snapshot()
            except Exception as e:
                logger.warning("[ContextAggregator] 会话快照失败: %s", e)
        ctx = {"history": history,
               "session": session_snap,
               "reference": reference or [],
               "snapshot_ts": time.time()}
        if self._event_bus is not None:
            try:
                from src.shared.events import CONTEXT_SNAPSHOT_READY
                self._event_bus.publish(CONTEXT_SNAPSHOT_READY, context=ctx)
            except Exception as e:
                logger.debug("[ContextAggregator] 快照事件发布失败: %s", e)
        return ctx
