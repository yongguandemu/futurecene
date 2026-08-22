"""command_router.py — 注册表驱动路由（规格书 4.5）

遍历注册表按能力匹配，无静态映射表：加分 brain 无需修改本文件 — 注册即路由。
D4 纪律：调用前必须检查开关。
llm:chat 注入：按身份区分注入 system_prompt（修复：智能助手误走角色世界书）——
@角色 定向（target_role）→ 注入对应角色人设与世界书；无定向 → 智能助手中立身份
（系统能力说明 + OBS 源，不注入任何角色设定），并注入对话历史（前端传入）。

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
from src.commander.session_context import VALID_ROLES
from src.shared.decision_log import OUTCOME_BLOCKED, OUTCOME_FAILED, record_decision
from src.shared.events import (
    COMMAND_COMPLETED,
    COMMAND_FAILED,
    COMMAND_RECEIVED,
    COMMAND_ROUTED,
    FRONTEND_SUBTITLE_UPDATE,
)

logger = logging.getLogger(__name__)

# 系统能力说明：注入 LLM，使其能回答"怎么使用系统"等真实问题（问题 3 修复）
_SYSTEM_CAPABILITIES = (
    "用户可以通过自然语言或「!指令」使用以下真实能力：\n"
    "- 查看系统状态：开关状态、调度官健康、成本统计（如「查看系统状态」）\n"
    "- 切换角色：yuki / lilith（如「切换到 Lilith」）\n"
    "- 日程设置：查看/添加/编辑直播日程（如「安排明天20:00的直播日程」）\n"
    "- 调度官管理：启停模块、检查健康状态（如「检查所有调度官健康状态」）\n"
    "- OBS 浏览器源：查询/打开直播叠加源（Live2D 模型、弹幕显示、弹幕输入、独立字幕），如「有哪些浏览器源」「打开字幕源」\n"
    "- 自由对话：直接输入任意文本聊天\n"
    "当用户询问如何使用本系统时，请基于以上真实能力回答，不要虚构或给出通用模板回答。"
)

# 智能助手（系统总控台）中立身份说明：不扮演任何虚拟主播角色，不注入角色世界书
# （修复：智能助手此前误走 session.role 默认 yuki 的角色人设与世界书）。
_ASSISTANT_NOTICE = (
    "你是 Future Scene 智能直播系统的直播智能助手（系统总控台）。"
    "你不是任何虚拟主播角色，不扮演主播人设，不代入任何角色的世界观，"
    "始终以直播管理助手的中立身份回答。\n"
    + _SYSTEM_CAPABILITIES
)

# 角色定向说明（@角色 定向）：以指定主播角色视角回答，保持人设与世界书设定
_ROLE_NOTICE = (
    "你正在以 {display_name}（{role}）的身份与观众互动，"
    "保持该角色的人设与世界观设定。\n"
    + _SYSTEM_CAPABILITIES
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
        """llm:chat 注入 system_prompt（身份区分 + 系统能力说明）与历史上下文。

        身份区分（修复：智能助手误走角色世界书）：
        - payload.target_role（前端 @角色 定向）→ 以指定角色注入人设 + 角色世界书；
        - 无定向 → 智能助手（系统总控台）中立身份，不注入任何角色世界书/人设。
        """
        if command.capability != "llm:chat":
            return
        # @角色 定向（command.py 透传）：仅接受合法角色；用后即删，payload 保持干净
        role = (command.payload.get("target_role") or "").strip().lower()
        command.payload.pop("target_role", None)
        if role and role not in VALID_ROLES:
            role = ""
        display_name = role
        role_prompt = ""
        if role and self._profile_loader is not None:
            try:
                profile = self._profile_loader.load(role)
                if profile is not None:
                    display_name = profile.display_name or role
                    role_prompt = profile.system_prompt or ""
            except Exception as e:
                logger.warning("[CommandRouter] 角色画像加载失败: %s", e)
        if role:
            # 角色视角对话：角色人设 + 定向说明 + 角色世界书核心设定
            notice = _ROLE_NOTICE.format(display_name=display_name, role=role)
            command.payload["system_prompt"] = (
                f"{role_prompt}\n\n{notice}" if role_prompt else notice)
            try:
                from src.shared.world_book import get_world_book
                wb_block = get_world_book().system_prompt_block(role)
                if wb_block:
                    command.payload["system_prompt"] += "\n\n" + wb_block
            except Exception as e:
                logger.debug("[CommandRouter] 世界书注入失败: %s", e)
        else:
            # 智能助手：中立身份，不注入角色人设与世界书
            command.payload["system_prompt"] = _ASSISTANT_NOTICE
        # OBS 直播浏览器源注入（保证 LLM 一定拿到源地址，不受世界书 1500 字符截断影响）
        try:
            from src.orchestrators.stream_orchestrator import obs_sources
            obs_lines = ["【OBS 直播浏览器源】"]
            for s in obs_sources.manifest():
                obs_lines.append("- {}({}): {}".format(s["name"], s["purpose"], s["url"]))
            command.payload["system_prompt"] += "\n\n" + "\n".join(obs_lines)
        except Exception as e:
            logger.debug("[CommandRouter] OBS 源注入失败: %s", e)
        # 历史上下文（前端 sendCommand 携带最近对话；_build_messages 组装 system→history→text）
        command.payload.setdefault("history", [])
        # 引擎路由：web 命令入口为日常对话，走 fast 引擎（DeepSeek V4 Flash 优先）
        command.payload["engine"] = "fast"

    async def _prepare_live(self, command: Command) -> Dict[str, Any]:
        """编排直播准备（直播间集成 · 智能助手联动）：
        live2d:load（当前角色）→ FRONTEND_SUBTITLE_UPDATE 确认字幕。
        load 失败不阻断（前端自行加载模型），仍返回就绪确认。
        """
        cid = command.command_id
        role = getattr(self._session, "role", "yuki") if self._session else "yuki"
        model_name = "Haru" if role == "lilith" else "Hiyori"
        orch = self._registry.match("live2d:load")
        load_ok = False
        if orch is not None and self._switch_manager.is_enabled(orch.name):
            try:
                res = await orch.handle({
                    "capability": "live2d:load",
                    "payload": {"role": role, "model_name": model_name},
                })
                load_ok = bool(res.get("ok"))
            except Exception as e:
                logger.warning("[CommandRouter] live2d:prepare load 失败: %s", e)
        text = ("直播界面已就绪，模型 %s 已装载，可以开播啦～" % model_name) if load_ok \
               else "直播界面已就绪（模型由前端加载），可以开播啦～"
        self._event_bus.publish(FRONTEND_SUBTITLE_UPDATE, text=text, role=role,
                                source="live2d:prepare")
        return {"ok": True, "command_id": cid,
                "data": {"prepared": True, "role": role, "model": model_name}}

    async def dispatch(self, command: Command) -> Dict[str, Any]:
        if not command.command_id:
            command.command_id = uuid.uuid4().hex
        cid = command.command_id
        self._inject_llm_context(command)

        # 直播准备编排（直播间集成）：load 当前角色模型 → 字幕确认
        if command.capability == "live2d:prepare":
            return await self._prepare_live(command)

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
