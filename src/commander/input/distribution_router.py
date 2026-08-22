"""distribution_router.py — 输入分发路由（总控调度化，规格 2026-08-22 任务一）

按输入类型 + 意图命中路由到目标：
- operator / system_loop(命中意图) → command_router.dispatch（意图解析）
- audience → danmaku_pipeline.execute_with
- system_loop(未命中意图) → archive（归档短期记忆，不触发响应）
- external_app / reference → 不在此分发（外部应用透传事件；reference 走上下文）

# 模块内容清单 — distribution_router

## 1. 模块身份标识
- 所属调度官：commander（input 域）
- 能力名：无（指挥官内部服务）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| 无 | - | - | - | 依赖注入 intent_parser/command_router/danmaku_pipeline/event_bus |

## 3. 输入契约
- route(envelope: InputEnvelope) -> dict

## 4. 输出契约
- 成功：{"ok", "target", "capability", "archived"}；archive 时 archived=True
- 失败：目标未注入 → {"ok": False, "target": "archive", "archived": True}

## 5. 依赖声明
- 外部服务：无
- 内部模块：intent_parser、input_classifier、shared.events（可选）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 目标未注入 | command_router/pipeline 为 None | 归档返回，不抛异常 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | - | 无状态（持有注入引用） |

## 8. 领域状态说明
- 状态项：无
- 持久化：无
"""
import asyncio
import logging
from typing import Any, Dict

from src.commander.input.input_classifier import InputEnvelope, InputType

logger = logging.getLogger(__name__)


class DistributionRouter:
    """按类型与意图分发输入。"""

    def __init__(self, intent_parser=None, command_router=None,
                 danmaku_pipeline=None, event_bus=None):
        self._parser = intent_parser
        self._cmd_router = command_router
        self._pipeline = danmaku_pipeline
        self._event_bus = event_bus

    async def route(self, envelope: InputEnvelope) -> Dict[str, Any]:
        t = envelope.input_type
        if t == InputType.REFERENCE:
            return {"ok": True, "target": "context", "capability": "",
                    "archived": False}
        if t == InputType.EXTERNAL_APP:
            # 外部应用事件透传（由对应域订阅消费），此处仅记录
            return {"ok": True, "target": "event_bus", "capability": "",
                    "archived": False}
        if t == InputType.AUDIENCE:
            if self._pipeline is None:
                return {"ok": False, "target": "archive", "capability": "",
                        "archived": True}
            text = envelope.payload.get("text", "")
            await self._pipeline.execute_with(
                text, role=envelope.meta.get("role", "yuki"))
            return {"ok": True, "target": "danmaku_pipeline",
                    "capability": "llm:chat", "archived": False}
        # operator / system_loop：意图解析 → 命中分发，未命中归档
        if self._parser is None or self._cmd_router is None:
            return {"ok": False, "target": "archive", "capability": "",
                    "archived": True}
        text = envelope.payload.get("text", "")
        cmd = self._parser.parse(text, source=envelope.source,
                                 session_id="default")
        if t == InputType.SYSTEM_LOOP and cmd.capability == "llm:chat":
            # 系统自循环无明确意图 → 归档短期记忆，不触发新一轮分发
            return {"ok": True, "target": "archive", "capability": "llm:chat",
                    "archived": True}
        result = await self._cmd_router.dispatch(cmd)
        if self._event_bus is not None:
            from src.shared.events import INPUT_ROUTED
            self._event_bus.publish(INPUT_ROUTED, target="command_router",
                                    capability=cmd.capability, archived=False,
                                    input_type=t.value)
        return {"ok": bool(result.get("ok")), "target": "command_router",
                "capability": cmd.capability, "archived": False}
