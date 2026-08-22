"""danmaku_pipeline.py — 弹幕→对话→TTS→Live2D 全链路编排（规格书 9.2）

订阅 danmaku:received，按验收链路顺序编排：
1. memory:retrieve（记忆上下文注入 history）
2. llm:chat（对话生成，engine=fast 低延迟）
3. FRONTEND_SUBTITLE_UPDATE（字幕）
4. tts:synthesize（发布 tts:audio_ready → Live2D 口型同步为表达领域订阅）
5. memory:store（本次对话入短期记忆）
6. SPEECH_COMPLETED（发言完成，多角色协作触发接话决策）

安全策略（ADR-007）：不设输入/输出安全过滤环节，内容安全信任厂商
（DeepSeek/智谱）安全系统；角色边界由世界书「人设唯一性」正向设定维持。

设计纪律：不直接 import 调度官模块（D2），通过注入实例调用 handle（命令调用）；
所有环节失败只记录日志并跳过（EventBus 不级联，规格书 9.3）。

# 模块内容清单（8 项契约）
1. 模块身份标识：commander · DanmakuPipeline · 对外 start()/stop()/execute_with()（订阅 danmaku:received）
2. 配置契约：无独立配置；依赖注入 llm/tts/memory 调度官实例、switch_manager 与 SessionContext（角色实时读取）
3. 输入契约：订阅 danmaku:received（content/user_name）；! 前缀系统命令跳过；execute_with(text, role, system_prompt, turn_context, user_name) 参数化入口
4. 输出契约：发布 FRONTEND_SUBTITLE_UPDATE/SPEECH_COMPLETED；调用各调度官 handle（memory/llm/tts）；触发 tts:audio_ready 供 Live2D 订阅；字幕/LLM/TTS/记忆/发言完成事件均携带发言角色（默认取 SessionContext 实时值，未注入回退 yuki）
5. 依赖声明：asyncio、logging、typing、shared.events、shared.decision_log
6. 错误定义：各环节失败仅记录日志并跳过（不级联）；LLM 未注入跳过
7. 生命周期方法：start()/stop()
8. 领域状态说明：_started 标记、注入的调度官引用（_llm/_tts/_memory）
"""
import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from src.shared.decision_log import (
    OUTCOME_ESCALATED, OUTCOME_NO_ACTION, record_decision,
)
from src.shared.events import (
    ACTIVE_DIALOGUE,
    DANMAKU_RECEIVED,
    FRONTEND_SUBTITLE_UPDATE,
    GIFT_RECEIVED,
    SPEECH_COMPLETED,
    SPEECH_SCHEDULED,
)
from src.shared.world_book import get_world_book
from src.commander.tool_registry import TOOL_CALL_RE

logger = logging.getLogger(__name__)

SESSION_ID = "default"

# 礼物感谢节流间隔（秒）：礼物事件密集时合并为一次 LLM 感谢，避免刷屏
GIFT_THANK_INTERVAL = 20.0

# LLM 工具调用最大轮数：防止循环调用失控
TOOL_MAX_ROUNDS = 3


class DanmakuPipeline:
    """弹幕对话管线（指挥官层订阅逻辑，P1 全链路版）。"""

    def __init__(self, event_bus, llm_orchestrator=None, tts_orchestrator=None,
                 memory_orchestrator=None, switch_manager=None, session=None,
                 profile_loader=None, tool_registry=None):
        self._event_bus = event_bus
        self._llm = llm_orchestrator
        self._tts = tts_orchestrator
        self._memory = memory_orchestrator
        self._switch_manager = switch_manager
        self._session = session  # SessionContext（可选注入，角色实时读取）
        self._profile_loader = profile_loader  # CharacterProfileLoader（可选注入）
        self._tool_registry = tool_registry  # LLM 工具注册表（可选注入，默认空）
        self._started = False
        self._last_gift_at = 0.0  # 礼物感谢节流时间戳
        self._scheduler = None  # SpeechScheduler（任务二：speech:scheduled 播放前合成 + complete）

    def set_speech_scheduler(self, scheduler) -> None:
        """注入 SpeechScheduler：speech:scheduled 播放完成后回执 complete(uid)。"""
        self._scheduler = scheduler

    def start(self) -> None:
        if self._started:
            return
        self._event_bus.subscribe(DANMAKU_RECEIVED, self._on_danmaku)
        self._event_bus.subscribe(GIFT_RECEIVED, self._on_gift)
        self._event_bus.subscribe(ACTIVE_DIALOGUE, self._on_active_dialogue)
        self._event_bus.subscribe(SPEECH_SCHEDULED, self._on_speech_scheduled)
        self._started = True
        logger.info("[DanmakuPipeline] 已订阅 danmaku:received / gift:received / dialogue:active / speech:scheduled")

    def stop(self) -> None:
        if self._started:
            self._event_bus.unsubscribe(DANMAKU_RECEIVED, self._on_danmaku)
            self._event_bus.unsubscribe(GIFT_RECEIVED, self._on_gift)
            self._event_bus.unsubscribe(ACTIVE_DIALOGUE, self._on_active_dialogue)
            self._event_bus.unsubscribe(SPEECH_SCHEDULED, self._on_speech_scheduled)
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
        if self._llm is None:
            # 入口守卫：LLM 未注入直接拒绝（避免 _chat 内 AttributeError 被吞后
            # 误记 llm_empty_reply；_on_danmaku 既有守卫保留，双保险）
            return {"ok": False, "error": "llm-not-injected"}
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
        """角色人设 system_prompt：角色画像 + 世界书核心设定 + 可用工具（未注入时返回空，不缓存）。

        role 为空时按当前会话角色（_current_role）读取，与既有注入逻辑一致。
        世界书按 metadata.role 注入本角色核心条目（身份/关系/行为），联通指挥官。
        """
        if self._profile_loader is None:
            return ""
        target = role or self._current_role()
        try:
            profile = self._profile_loader.load(target)
            base = profile.system_prompt if profile else ""
            block = get_world_book().system_prompt_block(target)
            if block:
                base = (base + "\n\n" + block).strip() if base else block
            # LLM 工具清单（P1：对话内工具调用）
            if self._tool_registry is not None:
                tools = self._tool_registry.prompt_block()
                if tools:
                    base = (base + "\n\n" + tools).strip() if base else tools
            return base
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

    def _on_gift(self, event: str, content: str = "", user_name: str = "",
                 **kwargs) -> None:
        """礼物事件：作为信息输入走 LLM 对话链路（与弹幕同管线）。

        构造「【礼物】XX 送出了 Y」输入，由角色人设 + 世界书自然生成感谢与互动；
        GIFT_THANK_INTERVAL 秒内只感谢一次（密集礼物合并），避免刷屏。
        """
        text = (content or "").strip()
        if not text or not user_name:
            return
        now = time.time()
        if now - self._last_gift_at < GIFT_THANK_INTERVAL:
            logger.debug("[DanmakuPipeline] 礼物感谢节流，跳过: %s", text[:20])
            return
        if self._llm is None:
            logger.debug("[DanmakuPipeline] LLM 未注入，礼物感谢跳过")
            return
        self._last_gift_at = now
        try:
            gift_input = "【礼物】{} 送出了 {}".format(user_name, text)
            asyncio.run(self.execute_with(gift_input, role=self._current_role(),
                                          user_name=user_name))
        except Exception as e:
            logger.error("[DanmakuPipeline] 礼物感谢异常: %s", e)

    def _on_active_dialogue(self, event: str, text: str = "", **kwargs) -> None:
        """冷场主动发言：字幕 + TTS 合成（Live2D 口型由 audio_ready 驱动）。"""
        text = (text or "").strip()
        if not text:
            return
        try:
            asyncio.run(self._speak_active(text))
        except Exception as e:
            logger.error("[DanmakuPipeline] 主动发言异常: %s", e)

    async def _speak_active(self, text: str) -> None:
        """主动发言：字幕 → TTS → 记忆 → 发言完成事件。"""
        role = self._current_role()
        # 1. 字幕事件
        self._event_bus.publish(FRONTEND_SUBTITLE_UPDATE,
                                text=text, role=role, source="active_dialogue")
        # 2. TTS 合成
        synth = await self._synthesize(text, role)
        audio_id = synth.get("audio_id", "") if synth else ""
        # 3. 入短期记忆（主动对话只存 assistant 消息）
        await self._store_active_memory(text, role)
        # 4. 发言完成事件（多角色协作触发接话决策）
        self._event_bus.publish(SPEECH_COMPLETED, role=role, text=text,
                                audio_id=audio_id)

    def _on_speech_scheduled(self, event: str, text: str = "", mood: str = "default",
                             role: str = "yuki", uid: str = "", **kwargs) -> None:
        """SpeechScheduler 排期发言（播放前合成，QA Q3）：字幕 → TTS → 完成回执。"""
        text = (text or "").strip()
        if not text:
            if uid:
                self._complete_speech(uid)
            return
        try:
            asyncio.run(self._speak_scheduled(text, role, uid))
        except Exception as e:
            logger.error("[DanmakuPipeline] 排期发言异常: %s", e)
            self._complete_speech(uid)

    async def _speak_scheduled(self, text: str, role: str, uid: str = "") -> None:
        """排期发言：字幕 → TTS（播放前合成）→ 完成事件 → scheduler 回执。"""
        self._event_bus.publish(FRONTEND_SUBTITLE_UPDATE,
                                text=text, role=role, source="speech_scheduler")
        synth = await self._synthesize(text, role)
        audio_id = synth.get("audio_id", "") if synth else ""
        await self._store_active_memory(text, role)
        self._event_bus.publish(SPEECH_COMPLETED, role=role, text=text,
                                audio_id=audio_id)
        self._complete_speech(uid)

    def _complete_speech(self, uid: str) -> None:
        if uid and self._scheduler is not None:
            try:
                self._scheduler.complete(uid)
            except Exception as e:
                logger.warning("[DanmakuPipeline] 发言完成回执失败 %s: %s", uid, e)

    # ---------- 全链路编排（规格书 9.2） ----------

    async def _process(self, text: str, role: str, system_prompt: str = "",
                       turn_context=None, user_name: str = "") -> Dict[str, Any]:
        turn_context = turn_context or []
        # 1. 记忆检索 → 上下文注入 history（按发言角色分桶）
        history = await self._retrieve_memory(text, role)

        # 2. LLM 对话（显式 system_prompt 优先，否则沿用 profile_loader 注入）
        reply_text = await self._chat(text, history, role, system_prompt, turn_context)
        # 2.1 工具调用循环（P1：LLM 输出 [[TOOL:name:arg]] → 执行 → 回填 → 再生成）
        if reply_text and self._tool_registry is not None:
            reply_text = await self._run_tool_loop(
                reply_text, text, history, role, system_prompt, turn_context)
        if not reply_text:
            record_decision(source="danmaku_pipeline", outcome=OUTCOME_NO_ACTION,
                            reason_code="llm_empty_reply",
                            layer="L3", capability="llm:chat",
                            detail="LLM 未产出回复，决定不回应: {}".format(text[:30]),
                            min_interval=10)
            return {"ok": False, "error": "llm-empty"}

        # 3. 字幕事件（前端 subtitle_overlay 消费，携带发言角色）
        self._event_bus.publish(FRONTEND_SUBTITLE_UPDATE,
                                text=reply_text, role=role,
                                user_name=user_name)

        # 4. TTS 合成（发布 tts:audio_ready → Live2D 口型，表达领域订阅）
        synth = await self._synthesize(reply_text, role)
        audio_id = synth.get("audio_id", "") if synth else ""

        # 5. 对话入短期记忆（按发言角色分桶）
        await self._store_memory(text, reply_text, role)

        # 6. 发言完成事件（多角色协作：触发接话决策，携带 role/text/audio_id）
        self._event_bus.publish(SPEECH_COMPLETED, role=role, text=reply_text,
                                audio_id=audio_id)
        return {"ok": True, "data": {"reply": reply_text, "audio_id": audio_id},
                "error": None}

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

    async def _chat(self, text: str, history: List[Dict[str, str]], role: str = "",
                    system_prompt: str = "", turn_context=None) -> str:
        try:
            payload = {"text": text, "role": role, "history": history,
                       "engine": "fast"}  # 弹幕互动走 fast 引擎（DeepSeek V4 Flash 优先，成本/延迟双优）
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

    async def _run_tool_loop(self, reply: str, text: str, history: List[Dict[str, str]],
                             role: str, system_prompt: str, turn_context) -> str:
        """工具调用循环：检测 [[TOOL:name:arg]] → 执行 → 结果回填 history → 再生成。

        最多 TOOL_MAX_ROUNDS 轮，防止 LLM 循环调用失控。
        """
        current = reply
        for _ in range(TOOL_MAX_ROUNDS):
            m = TOOL_CALL_RE.search(current or "")
            if not m:
                return (current or "").strip()
            name, arg = m.group(1), m.group(2)
            if not self._tool_registry.has(name):
                return (current or "").strip()  # 未注册工具：不拦截，原样返回
            result = self._tool_registry.execute(name, arg)
            logger.info("[DanmakuPipeline] 工具调用 %s(%s) → %s", name, arg[:30], result[:50])
            history = history + [
                {"role": "assistant", "content": current},
                {"role": "user", "content": "工具 {} 结果：{}".format(name, result)}]
            current = await self._chat(text, history, role, system_prompt, turn_context)
            if not current:
                return ""
        return (current or "").strip()

    async def _check_output(self, reply_text: str) -> bool:
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

    async def _store_active_memory(self, text: str, role: str = "") -> None:
        """主动对话记忆：只存 assistant 消息（无用户输入）。"""
        if self._memory is None:
            return
        try:
            payload = {"content": text, "role": "assistant", "session_id": SESSION_ID}
            if role:
                payload["character_id"] = role
            await self._memory.handle({
                "capability": "memory:store",
                "payload": payload,
            })
        except Exception as e:
            logger.error("[DanmakuPipeline] 主动对话记忆存储失败: %s", e)
