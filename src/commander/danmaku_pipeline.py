"""danmaku_pipeline.py — 弹幕→对话→TTS→Live2D 全链路编排（规格书 9.2）

订阅 danmaku:received，按验收链路顺序编排：
1. safety:check_input（输入安全，block → 发布 audience:filtered 拦截）
2. memory:retrieve（记忆上下文注入 history）
3. llm:chat（对话生成）
4. safety:check_output（输出安全）
5. FRONTEND_SUBTITLE_UPDATE（字幕）
6. tts:synthesize（发布 tts:audio_ready → Live2D 口型同步为表达领域订阅）
7. memory:store（本次对话入短期记忆）
8. SPEECH_COMPLETED（发言完成，多角色协作触发接话决策）

设计纪律：不直接 import 调度官模块（D2），通过注入实例调用 handle（命令调用）；
所有环节失败只记录日志并跳过（EventBus 不级联，规格书 9.3）。

# 模块内容清单（8 项契约）
1. 模块身份标识：commander · DanmakuPipeline · 对外 start()/stop()/execute_with()（订阅 danmaku:received）
2. 配置契约：无独立配置；依赖注入 llm/tts/safety/memory 调度官实例、switch_manager 与 SessionContext（角色实时读取）
3. 输入契约：订阅 danmaku:received（content/user_name）；! 前缀系统命令跳过；execute_with(text, role, system_prompt, turn_context, user_name) 参数化入口
4. 输出契约：发布 FRONTEND_SUBTITLE_UPDATE/AUDIENCE_FILTERED/SPEECH_COMPLETED；调用各调度官 handle（safety/memory/llm/tts）；触发 tts:audio_ready 供 Live2D 订阅；字幕/LLM/TTS/记忆/发言完成事件均携带发言角色（默认取 SessionContext 实时值，未注入回退 yuki）
5. 依赖声明：asyncio、logging、typing、shared.events
6. 错误定义：各环节失败仅记录日志并跳过（不级联）；LLM 未注入跳过
7. 生命周期方法：start()/stop()
8. 领域状态说明：_started 标记、注入的调度官引用（_llm/_tts/_safety/_memory）
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

from src.shared.decision_log import (
    OUTCOME_BLOCKED, OUTCOME_ESCALATED, OUTCOME_NO_ACTION, record_decision,
)
from src.shared.events import (
    AUDIENCE_FILTERED,
    DANMAKU_RECEIVED,
    FRONTEND_SUBTITLE_UPDATE,
    SPEECH_COMPLETED,
)

logger = logging.getLogger(__name__)

SESSION_ID = "default"


class DanmakuPipeline:
    """弹幕对话管线（指挥官层订阅逻辑，P1 全链路版）。"""

    def __init__(self, event_bus, llm_orchestrator=None, tts_orchestrator=None,
                 safety_orchestrator=None, memory_orchestrator=None,
                 switch_manager=None, session=None, profile_loader=None):
        self._event_bus = event_bus
        self._llm = llm_orchestrator
        self._tts = tts_orchestrator
        self._safety = safety_orchestrator
        self._memory = memory_orchestrator
        self._switch_manager = switch_manager
        self._session = session  # SessionContext（可选注入，角色实时读取）
        self._profile_loader = profile_loader  # CharacterProfileLoader（可选注入）
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._event_bus.subscribe(DANMAKU_RECEIVED, self._on_danmaku)
        self._started = True
        logger.info("[DanmakuPipeline] 已订阅 danmaku:received（P1 全链路）")

    def stop(self) -> None:
        if self._started:
            self._event_bus.unsubscribe(DANMAKU_RECEIVED, self._on_danmaku)
            self._started = False

    async def execute_with(self, text: str, role: str, system_prompt: str = "",
                           turn_context=None, user_name: str = "") -> Dict[str, Any]:
        """参数化入口：按指定角色 + 人格 + 话轮上下文执行全链路。

        单角色模式默认路径（_on_danmaku）调用本方法（role=_current_role()）保持一致；
        多角色模式由协作协调器 await 调用（role 显式指定，协调器已驱动事件循环）。
        显式传入 system_prompt 优先，否则沿用 profile_loader 注入逻辑。
        """
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "text 必填"}
        try:
            return await self._process(text, role, system_prompt,
                                       turn_context or [], user_name)
        except Exception as e:
            logger.error("[DanmakuPipeline] execute_with 异常: %s", e)
            return {"ok": False, "error": str(e)}

    # ---------- 入口（EventBus 同步回调） ----------

    def _current_role(self) -> str:
        """当前角色：实时读取 SessionContext，未注入时回退 yuki（不缓存）。"""
        if self._session is not None:
            return getattr(self._session, "role", "yuki") or "yuki"
        return "yuki"

    def _system_prompt(self, role: str = "") -> str:
        """角色人设 system_prompt：实时读取角色画像，未注入时返回空（不缓存）。

        role 为空时按当前会话角色（_current_role）读取，与既有注入逻辑一致。
        """
        if self._profile_loader is None:
            return ""
        target = role or self._current_role()
        try:
            profile = self._profile_loader.load(target)
            return profile.system_prompt if profile else ""
        except Exception as e:
            logger.warning("[DanmakuPipeline] 角色画像加载失败: %s", e)
            return ""

    def _on_danmaku(self, event: str, content: str, user_name: str = "", **kwargs) -> None:
        text = (content or "").strip()
        if not text:
            return
        if text.startswith("!"):
            # 系统命令：交由 Intent Parser / 指挥官处理（M3 接入），管线跳过
            logger.debug("[DanmakuPipeline] 系统命令跳过: %s", text[:20])
            record_decision(source="danmaku_pipeline", outcome=OUTCOME_ESCALATED,
                            reason_code="system_command_escalated",
                            layer="L3", capability="commander:intent",
                            detail="!命令上抛指挥官: {}".format(text[:20]))
            return
        if self._llm is None:
            logger.debug("[DanmakuPipeline] LLM 未注入，跳过")
            record_decision(source="danmaku_pipeline", outcome=OUTCOME_NO_ACTION,
                            reason_code="llm_not_injected",
                            layer="L3", capability="llm:chat",
                            detail="LLM 调度官未注入，决定不回复: {}".format(text[:30]),
                            min_interval=30)
            return
        # 默认路径与多角色模式一致：走参数化入口（role=当前会话角色）
        try:
            asyncio.run(self.execute_with(text, role=self._current_role(),
                                          user_name=user_name))
        except Exception as e:
            logger.error("[DanmakuPipeline] 链路异常: %s", e)

    # ---------- 全链路编排（规格书 9.2） ----------

    async def _process(self, text: str, role: str, system_prompt: str = "",
                       turn_context=None, user_name: str = "") -> Dict[str, Any]:
        turn_context = turn_context or []
        # 1. 输入安全过滤
        if not await self._check_input(text):
            return {"ok": False, "error": "input-blocked"}

        # 2. 记忆检索 → 上下文注入 history（按发言角色分桶）
        history = await self._retrieve_memory(text, role)

        # 3. LLM 对话（显式 system_prompt 优先，否则沿用 profile_loader 注入）
        reply_text = await self._chat(text, history, role, system_prompt, turn_context)
        if not reply_text:
            record_decision(source="danmaku_pipeline", outcome=OUTCOME_NO_ACTION,
                            reason_code="llm_empty_reply",
                            layer="L3", capability="llm:chat",
                            detail="LLM 未产出回复，决定不回应: {}".format(text[:30]),
                            min_interval=10)
            return {"ok": False, "error": "llm-empty"}

        # 4. 输出安全过滤
        if not await self._check_output(reply_text):
            return {"ok": False, "error": "output-blocked"}

        # 5. 字幕事件（前端 subtitle_overlay 消费，携带发言角色）
        self._event_bus.publish(FRONTEND_SUBTITLE_UPDATE,
                                text=reply_text, role=role,
                                user_name=user_name)

        # 6. TTS 合成（发布 tts:audio_ready → Live2D 口型，表达领域订阅）
        synth = await self._synthesize(reply_text, role)
        audio_id = synth.get("audio_id", "") if synth else ""

        # 7. 对话入短期记忆（按发言角色分桶）
        await self._store_memory(text, reply_text, role)

        # 8. 发言完成事件（多角色协作：触发接话决策，携带 role/text/audio_id）
        self._event_bus.publish(SPEECH_COMPLETED, role=role, text=reply_text,
                                audio_id=audio_id)
        return {"ok": True, "data": {"reply": reply_text, "audio_id": audio_id},
                "error": None}

    async def _check_input(self, text: str) -> bool:
        if self._safety is None:
            return True
        result = await self._safety.handle({
            "capability": "safety:check_input",
            "payload": {"text": text, "source": "danmaku"},
        })
        verdict = result.get("data", {}).get("verdict", "allow")
        if verdict != "allow":
            logger.warning("[DanmakuPipeline] 输入被拦截: %s", result.get("data", {}).get("reason"))
            self._event_bus.publish(AUDIENCE_FILTERED, content=text,
                                    reason=result.get("data", {}).get("reason"))
            record_decision(source="danmaku_pipeline", outcome=OUTCOME_BLOCKED,
                            reason_code="safety_input_blocked",
                            layer="L0", capability="safety:check_input",
                            detail="输入被硬规则拦截: {}".format(
                                result.get("data", {}).get("reason", "")),
                            min_interval=10)
            return False
        return True

    async def _retrieve_memory(self, text: str, role: str = "") -> List[Dict[str, str]]:
        if self._memory is None:
            return []
        try:
            payload = {"query": text, "k": 3, "session_id": SESSION_ID}
            if role:
                payload["character_id"] = role  # 记忆按发言角色分桶
            result = await self._memory.handle({
                "capability": "memory:retrieve",
                "payload": payload,
            })
            memories = result.get("data", {}).get("memories", [])
            # 注入为 assistant 历史（轻量，不放大对象）
            return [{"role": "assistant", "content": m["content"]} for m in memories[:3]]
        except Exception as e:
            logger.error("[DanmakuPipeline] 记忆检索失败: %s", e)
            return []

    async def _chat(self, text: str, history, role: str = "", system_prompt: str = "",
                    turn_context=None) -> str:
        try:
            payload = {"text": text, "role": role, "history": history}
            if system_prompt:
                # 显式传入（多角色协调器构造，含感知彼此等）优先，不叠加 profile_loader 注入
                payload["system_prompt"] = system_prompt
            elif self._profile_loader is not None:
                # 既有注入逻辑：按角色画像实时读取
                payload["system_prompt"] = self._system_prompt(role)
            if turn_context:
                payload["turn_context"] = turn_context
            result = await self._llm.handle({
                "capability": "llm:chat",
                "payload": payload,
            })
        except Exception as e:
            logger.error("[DanmakuPipeline] LLM 调用异常: %s", e)
            return ""
        if not result.get("ok"):
            logger.warning("[DanmakuPipeline] LLM 未返回成功: %s", result.get("error"))
            return ""
        reply = result.get("data", {}).get("reply", "")
        return (reply or "").strip()

    async def _check_output(self, reply_text: str) -> bool:
        if self._safety is None:
            return True
        result = await self._safety.handle({
            "capability": "safety:check_output",
            "payload": {"text": reply_text},
        })
        verdict = result.get("data", {}).get("verdict", "allow")
        if verdict != "allow":
            logger.warning("[DanmakuPipeline] 输出被拦截，不推送字幕")
            record_decision(source="danmaku_pipeline", outcome=OUTCOME_BLOCKED,
                            reason_code="safety_output_blocked",
                            layer="L0", capability="safety:check_output",
                            detail="输出被硬规则拦截，不推送字幕: {}".format(
                                result.get("data", {}).get("reason", "")),
                            min_interval=10)
            return False
        return True

    async def _synthesize(self, reply_text: str, role: str = "") -> Optional[Dict]:
        if self._tts is None:
            return None
        try:
            # 长文本自动分片合成（tts:stream_synthesize），避免单次合成超时
            capability = "tts:stream_synthesize" if len(reply_text) > 80 else "tts:synthesize"
            result = await self._tts.handle({
                "capability": capability,
                "payload": {"text": reply_text, "role": role or self._current_role()},
            })
            if result.get("ok"):
                logger.info("[DanmakuPipeline] TTS 合成完成 audio_id=%s",
                            result.get("data", {}).get("audio_id"))
                return result.get("data", {})
            return None
        except Exception as e:
            logger.error("[DanmakuPipeline] TTS 调用异常: %s", e)
            return None

    async def _store_memory(self, text: str, reply_text: str, role: str = "") -> None:
        if self._memory is None:
            return
        try:
            # 注意：role（发言角色）映射为 character_id 分桶；role=user/assistant 为消息角色，勿混淆
            user_payload = {"content": text, "role": "user", "session_id": SESSION_ID}
            assistant_payload = {"content": reply_text, "role": "assistant",
                                 "session_id": SESSION_ID}
            if role:
                user_payload["character_id"] = role
                assistant_payload["character_id"] = role
            await self._memory.handle({
                "capability": "memory:store",
                "payload": user_payload,
            })
            await self._memory.handle({
                "capability": "memory:store",
                "payload": assistant_payload,
            })
        except Exception as e:
            logger.error("[DanmakuPipeline] 记忆存储失败: %s", e)
