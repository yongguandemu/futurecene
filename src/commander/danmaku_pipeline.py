"""danmaku_pipeline.py — 弹幕→对话→TTS→Live2D 全链路编排（规格书 9.2）

订阅 danmaku:received，按验收链路顺序编排：
1. safety:check_input（输入安全，block → 发布 audience:filtered 拦截）
2. memory:retrieve（记忆上下文注入 history）
3. llm:chat（对话生成）
4. safety:check_output（输出安全）
5. FRONTEND_SUBTITLE_UPDATE（字幕）
6. tts:synthesize（发布 tts:audio_ready → Live2D 口型同步为表达领域订阅）
7. memory:store（本次对话入短期记忆）

设计纪律：不直接 import 调度官模块（D2），通过注入实例调用 handle（命令调用）；
所有环节失败只记录日志并跳过（EventBus 不级联，规格书 9.3）。

# 模块内容清单（8 项契约）
1. 模块身份标识：commander · DanmakuPipeline · 对外 start()/stop()（订阅 danmaku:received）
2. 配置契约：无独立配置；依赖注入 llm/tts/safety/memory 调度官实例与 switch_manager
3. 输入契约：订阅 danmaku:received（content/user_name）；! 前缀系统命令跳过
4. 输出契约：发布 FRONTEND_SUBTITLE_UPDATE/AUDIENCE_FILTERED；调用各调度官 handle（safety/memory/llm/tts）；触发 tts:audio_ready 供 Live2D 订阅
5. 依赖声明：asyncio、logging、typing、shared.events
6. 错误定义：各环节失败仅记录日志并跳过（不级联）；LLM 未注入跳过
7. 生命周期方法：start()/stop()
8. 领域状态说明：_started 标记、注入的调度官引用（_llm/_tts/_safety/_memory）
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

from src.shared.events import (
    AUDIENCE_FILTERED,
    DANMAKU_RECEIVED,
    FRONTEND_SUBTITLE_UPDATE,
)

logger = logging.getLogger(__name__)

SESSION_ID = "default"


class DanmakuPipeline:
    """弹幕对话管线（指挥官层订阅逻辑，P1 全链路版）。"""

    def __init__(self, event_bus, llm_orchestrator=None, tts_orchestrator=None,
                 safety_orchestrator=None, memory_orchestrator=None,
                 switch_manager=None):
        self._event_bus = event_bus
        self._llm = llm_orchestrator
        self._tts = tts_orchestrator
        self._safety = safety_orchestrator
        self._memory = memory_orchestrator
        self._switch_manager = switch_manager
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

    # ---------- 入口（EventBus 同步回调） ----------

    def _on_danmaku(self, event: str, content: str, user_name: str = "", **kwargs) -> None:
        text = (content or "").strip()
        if not text:
            return
        if text.startswith("!"):
            # 系统命令：交由 Intent Parser / 指挥官处理（M3 接入），管线跳过
            logger.debug("[DanmakuPipeline] 系统命令跳过: %s", text[:20])
            return
        if self._llm is None:
            logger.debug("[DanmakuPipeline] LLM 未注入，跳过")
            return
        try:
            asyncio.run(self._process(text, user_name))
        except Exception as e:
            logger.error("[DanmakuPipeline] 链路异常: %s", e)

    # ---------- 全链路编排（规格书 9.2） ----------

    async def _process(self, text: str, user_name: str) -> None:
        # 1. 输入安全过滤
        if not await self._check_input(text):
            return

        # 2. 记忆检索 → 上下文注入 history
        history = await self._retrieve_memory(text)

        # 3. LLM 对话
        reply_text = await self._chat(text, history)
        if not reply_text:
            return

        # 4. 输出安全过滤
        if not await self._check_output(reply_text):
            return

        # 5. 字幕事件（前端 subtitle_overlay 消费）
        self._event_bus.publish(FRONTEND_SUBTITLE_UPDATE,
                                text=reply_text, role="yuki", user_name=user_name)

        # 6. TTS 合成（发布 tts:audio_ready → Live2D 口型，表达领域订阅）
        await self._synthesize(reply_text)

        # 7. 对话入短期记忆
        await self._store_memory(text, reply_text)

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
            return False
        return True

    async def _retrieve_memory(self, text: str) -> List[Dict[str, str]]:
        if self._memory is None:
            return []
        try:
            result = await self._memory.handle({
                "capability": "memory:retrieve",
                "payload": {"query": text, "k": 3, "session_id": SESSION_ID},
            })
            memories = result.get("data", {}).get("memories", [])
            # 注入为 assistant 历史（轻量，不放大对象）
            return [{"role": "assistant", "content": m["content"]} for m in memories[:3]]
        except Exception as e:
            logger.error("[DanmakuPipeline] 记忆检索失败: %s", e)
            return []

    async def _chat(self, text: str, history: List[Dict[str, str]]) -> str:
        try:
            result = await self._llm.handle({
                "capability": "llm:chat",
                "payload": {"text": text, "role": "yuki", "history": history},
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
            return False
        return True

    async def _synthesize(self, reply_text: str) -> None:
        if self._tts is None:
            return
        try:
            # 长文本自动分片合成（tts:stream_synthesize），避免单次合成超时
            capability = "tts:stream_synthesize" if len(reply_text) > 80 else "tts:synthesize"
            result = await self._tts.handle({
                "capability": capability,
                "payload": {"text": reply_text, "role": "yuki"},
            })
            if result.get("ok"):
                logger.info("[DanmakuPipeline] TTS 合成完成 audio_id=%s",
                            result.get("data", {}).get("audio_id"))
        except Exception as e:
            logger.error("[DanmakuPipeline] TTS 调用异常: %s", e)

    async def _store_memory(self, text: str, reply_text: str) -> None:
        if self._memory is None:
            return
        try:
            await self._memory.handle({
                "capability": "memory:store",
                "payload": {"content": text, "role": "user", "session_id": SESSION_ID},
            })
            await self._memory.handle({
                "capability": "memory:store",
                "payload": {"content": reply_text, "role": "assistant",
                            "session_id": SESSION_ID},
            })
        except Exception as e:
            logger.error("[DanmakuPipeline] 记忆存储失败: %s", e)
