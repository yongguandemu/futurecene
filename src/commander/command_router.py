"""command_router.py — 注册表驱动路由（规格书 4.5）

遍历注册表按能力匹配，无静态映射表：加分 brain 无需修改本文件 — 注册即路由。
D4 纪律：调用前必须检查开关。
llm:chat 注入：按当前会话角色注入 system_prompt（角色画像 + 系统能力说明）与对话历史（前端传入），
保证助手知道自己的身份与系统实际能力（修复：通用回答问题）。

# 模块内容清单（8 项契约）
1. 模块身份标识：commander · CommandRouter · 对外 dispatch(command)
2. 配置契约：无独立配置；依赖注册表/开关管理器/事件总线；可选注入 profile_loader/session（llm:chat 注入用）
3. 输入契约：dispatch(Command) 结构化命令（capability + payload）
4. 输出契约：{ok, data, error, command_id}；发布 COMMAND_RECEIVED/COMMAND_ROUTED/COMMAND_COMPLETED/COMMAND_FAILED 事件（均携带 command_id）
5. 依赖声明：logging、typing、intent_parser.Command、shared.events
6. 错误定义：unknown capability / orchestrator disabled 返回 error；调度官执行异常捕获并发布 COMMAND_FAILED
7. 生命周期方法：dispatch()（async 路由入口）
8. 领域状态说明：无状态，持有 _registry/_switch_manager/_event_bus/_profile_loader/_session 引用
"""
import logging
import uuid
from typing import Any, Dict, Optional

from src.commander.intent_parser import Command
from src.shared.decision_log import OUTCOME_BLOCKED, OUTCOME_FAILED, record_decision
from src.shared.events import (
    COMMAND_COMPLETED,
    COMMAND_FAILED,
    COMMAND_RECEIVED,
    COMMAND_ROUTED,
)

logger = logging.getLogger(__name__)

# 系统能力说明：注入 LLM，使其能回答"怎么使用系统"等真实问题（问题 3 修复）
_SYSTEM_CAPABILITY_NOTICE = (
    "你运行在 Future Scene 智能直播系统中，当前人设角色为 {display_name}（{role}）。\n"
    "用户可以通过自然语言或「!指令」使用以下真实能力：\n"
    "- 查看系统状态：开关状态、调度官健康、成本统计（如「查看系统状态」）\n"
    "- 切换角色：yuki / lilith（如「切换到 Lilith」）\n"
    "- 日程设置：查看/添加/编辑直播日程（如「安排明天20:00的直播日程」）\n"
    "- 调度官管理：启停模块、检查健康状态（如「检查所有调度官健康状态」）\n"
    "- 自由对话：直接输入任意文本聊天\n"
    "当用户询问如何使用本系统时，请基于以上真实能力回答，不要虚构或给出通用模板回答。"
)


class CommandRouter:
    """注册表驱动路由。"""

    def __init__(self, registry, switch_manager, event_bus,
                 profile_loader=None, session=None):
        self._registry = registry
        self._switch_manager = switch_manager
        self._event_bus = event_bus
        self._profile_loader = profile_loader  # CharacterProfileLoader（可选）
        self._session = session  # SessionContext（可选，llm:chat 注入用）

    def _inject_llm_context(self, command: Command) -> None:
        """llm:chat 注入 system_prompt（角色画像 + 系统能力说明）与历史上下文。"""
        if command.capability != "llm:chat":
            return
        role = getattr(self._session, "role", "yuki") if self._session else "yuki"
        display_name = role
        role_prompt = ""
        if self._profile_loader is not None:
            try:
                profile = self._profile_loader.load(role)
                if profile is not None:
                    display_name = profile.display_name or role
                    role_prompt = profile.system_prompt or ""
            except Exception as e:
                logger.warning("[CommandRouter] 角色画像加载失败: %s", e)
        notice = _SYSTEM_CAPABILITY_NOTICE.format(display_name=display_name, role=role)
        command.payload["system_prompt"] = (
            f"{role_prompt}\n\n{notice}" if role_prompt else notice)
        # 历史上下文（前端 sendCommand 携带最近对话；_build_messages 组装 system→history→text）
        command.payload.setdefault("history", [])

    async def dispatch(self, command: Command) -> Dict[str, Any]:
        if not command.command_id:
            command.command_id = uuid.uuid4().hex
        cid = command.command_id
        self._inject_llm_context(command)
        self._event_bus.publish(COMMAND_RECEIVED, command=command,
                                command_id=cid)

        # D4 纪律：调用前必须检查开关
        orch = self._registry.match(command.capability)
        if orch is None:
            record_decision(source="command_router", outcome=OUTCOME_FAILED,
                            reason_code="unknown_capability",
                            layer="L3", capability=command.capability,
                            detail="指挥官无法路由，无该能力注册",
                            decision_id=cid)
            return {"ok": False, "error": f"unknown capability: {command.capability}",
                    "command_id": cid}
        if not self._switch_manager.is_enabled(orch.name):
            record_decision(source="command_router", outcome=OUTCOME_BLOCKED,
                            reason_code="orchestrator_disabled",
                            layer="L1", capability=command.capability,
                            detail="调度官开关关闭（授权收回），拒绝执行: {}".format(orch.name),
                            decision_id=cid)
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
