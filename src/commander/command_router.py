"""command_router.py — 注册表驱动路由（规格书 4.5）

遍历注册表按能力匹配，无静态映射表：加分 brain 无需修改本文件 — 注册即路由。
D4 纪律：调用前必须检查开关。

# 模块内容清单（8 项契约）
1. 模块身份标识：commander · CommandRouter · 对外 dispatch(command)
2. 配置契约：无独立配置；依赖注册表/开关管理器/事件总线
3. 输入契约：dispatch(Command) 结构化命令（capability + payload）
4. 输出契约：{ok, data, error, command_id}；发布 COMMAND_RECEIVED/COMMAND_ROUTED/COMMAND_COMPLETED/COMMAND_FAILED 事件（均携带 command_id）
5. 依赖声明：logging、typing、intent_parser.Command、shared.events
6. 错误定义：unknown capability / orchestrator disabled 返回 error；调度官执行异常捕获并发布 COMMAND_FAILED
7. 生命周期方法：dispatch()（async 路由入口）
8. 领域状态说明：无状态，持有 _registry/_switch_manager/_event_bus 引用
"""
import logging
import uuid
from typing import Any, Dict

from src.commander.intent_parser import Command
from src.shared.events import (
    COMMAND_COMPLETED,
    COMMAND_FAILED,
    COMMAND_RECEIVED,
    COMMAND_ROUTED,
)

logger = logging.getLogger(__name__)


class CommandRouter:
    """注册表驱动路由。"""

    def __init__(self, registry, switch_manager, event_bus):
        self._registry = registry
        self._switch_manager = switch_manager
        self._event_bus = event_bus

    async def dispatch(self, command: Command) -> Dict[str, Any]:
        if not command.command_id:
            command.command_id = uuid.uuid4().hex
        cid = command.command_id
        self._event_bus.publish(COMMAND_RECEIVED, command=command,
                                command_id=cid)

        # D4 纪律：调用前必须检查开关
        orch = self._registry.match(command.capability)
        if orch is None:
            return {"ok": False, "error": f"unknown capability: {command.capability}",
                    "command_id": cid}
        if not self._switch_manager.is_enabled(orch.name):
            return {"ok": False, "error": f"orchestrator disabled: {orch.name}",
                    "command_id": cid}

        self._event_bus.publish(COMMAND_ROUTED, capability=command.capability,
                                target=orch.name, command_id=cid)
        try:
            result = await orch.handle({"capability": command.capability,
                                        "payload": command.payload,
                                        "command_id": cid})
            result.setdefault("command_id", cid)
            self._event_bus.publish(COMMAND_COMPLETED, capability=command.capability,
                                    result=result, command_id=cid)
            return result
        except Exception as e:
            logger.error("[CommandRouter] 调度官 %s 执行异常: %s", orch.name, e)
            self._event_bus.publish(COMMAND_FAILED, capability=command.capability,
                                    error=str(e), command_id=cid)
            return {"ok": False, "error": str(e), "command_id": cid}
