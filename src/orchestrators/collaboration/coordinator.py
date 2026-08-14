"""coordinator.py — 多角色协作顶层协调器。

组装 arbitrator/turn_tracker/context_manager/triggers，订阅事件并驱动
DanmakuPipeline.execute_with 按角色执行。对外统一接口：
handle_danmaku / handle_speech_completed / request_utterance / snapshot / start / stop。

事件接线：
- danmaku:received         → 仲裁（arbitrate）→ _execute 按角色执行
- speech:completed         → 记录话轮 + triggers 评估 → 发布 collab:utterance_requested
- collab:utterance_requested → request_utterance → 回仲裁（冷却/互斥约束）→ 执行

互斥语义：arbitrate 在 acquire 成功时立即放行并持锁至 _execute 派发瞬间，
_execute 内 record_turn 后立即 release（执行是异步的，先放行队列），随后
_drain_queue 排空待发队列（最高优先级者先行），避免队列阻塞后续请求。
"""
import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from src.orchestrators.collaboration.arbitrator import SpeakerArbitrator
from src.orchestrators.collaboration.context_manager import ContextManager
from src.orchestrators.collaboration.triggers import CollabTriggers
from src.orchestrators.collaboration.turn_tracker import TurnTracker
from src.shared.events import (
    COLLAB_UTTERANCE_REQUESTED,
    DANMAKU_RECEIVED,
    SPEECH_COMPLETED,
)

logger = logging.getLogger(__name__)

_DEFAULT_RULES_ORDER = ["mention", "intent", "relevance", "cooldown", "random"]


class CollaborationCoordinator:
    def __init__(self, event_bus, pipeline=None, profiles=None,
                 session=None, live2d=None,
                 lead_role: str = "yuki", rules_order: Optional[List[str]] = None,
                 trigger_probability: float = 0.3,
                 trigger_global_cooldown: float = 20.0,
                 awareness_enabled: bool = True, seed: Optional[int] = None):
        self._event_bus = event_bus
        self._pipeline = pipeline
        self._profiles = profiles
        self._session = session
        self._live2d = live2d
        self._tt = TurnTracker()
        self._ctx = ContextManager()
        self._arb = SpeakerArbitrator(event_bus, self._tt, profiles=profiles,
                                      lead_role=lead_role, rules_order=rules_order,
                                      seed=seed)
        self._triggers = CollabTriggers(
            probability=trigger_probability, global_cooldown=trigger_global_cooldown,
            present_roles=(set(profiles.all_roles()) if profiles else {"yuki", "lilith"}),
            seed=seed)
        self._awareness = awareness_enabled
        self._runtime = {"trigger_probability": float(trigger_probability),
                         "trigger_global_cooldown": float(trigger_global_cooldown),
                         "lead_role": lead_role,
                         "awareness_enabled": bool(awareness_enabled),
                         "rules_order": list(rules_order or _DEFAULT_RULES_ORDER)}
        self._started = False

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._started:
            return
        self._event_bus.subscribe(DANMAKU_RECEIVED, self._on_danmaku)
        self._event_bus.subscribe(SPEECH_COMPLETED, self._on_speech_completed)
        self._event_bus.subscribe(COLLAB_UTTERANCE_REQUESTED, self._on_utterance_requested)
        self._started = True
        logger.info("[Collaboration] 已启动（订阅 danmaku/speech/completed/utterance）")

    def stop(self) -> None:
        if not self._started:
            return
        self._event_bus.unsubscribe(DANMAKU_RECEIVED, self._on_danmaku)
        self._event_bus.unsubscribe(SPEECH_COMPLETED, self._on_speech_completed)
        self._event_bus.unsubscribe(COLLAB_UTTERANCE_REQUESTED, self._on_utterance_requested)
        self._started = False

    # ---------- 事件入口（EventBus 同步回调） ----------

    def _on_danmaku(self, event: str, content: str, user_name: str = "", **kw) -> None:
        text = (content or "").strip()
        if not text or text.startswith("!"):
            return
        verdict = self._arb.arbitrate("danmaku", text, user_name, kind="danmaku")
        if verdict.role:
            self._execute(verdict.role, text, kind="danmaku")

    def _on_speech_completed(self, event: str, role: str, text: str = "",
                             audio_id: str = "", **kw) -> None:
        if not self._started:
            return
        self._ctx.record_turn(role, text or audio_id)
        self._tt.record_turn(role, "speech", text=text)
        props = self._triggers.evaluate(role, text)
        for p in props:
            self._event_bus.publish(COLLAB_UTTERANCE_REQUESTED, **p)

    def _on_utterance_requested(self, event: str, role: str, kind: str = "banter",
                                reason: str = "", ref_text: str = "", **kw) -> None:
        self.request_utterance(role, kind, reason, ref_text)

    # ---------- 对外接口 ----------

    def request_utterance(self, role: str, kind: str, reason: str = "",
                          ref_text: str = "") -> None:
        """联动发言请求（triggers/外部调用）→ 仲裁 → 执行。"""
        verdict = self._arb.arbitrate("collab", ref_text or "接个话", "",
                                      kind="collab", requester_role=role,
                                      ref_text=ref_text)
        if verdict.role:
            self._execute(verdict.role, ref_text or "接个话", kind=kind)

    def update_runtime(self, **kwargs) -> Dict[str, Any]:
        """运行时调参（POST /api/collab/config 白名单）。"""
        allowed = {"trigger_probability", "trigger_global_cooldown",
                   "lead_role", "awareness_enabled", "rules_order"}
        for k, v in kwargs.items():
            if k in allowed:
                self._runtime[k] = v
        self._triggers.update_runtime(
            float(self._runtime["trigger_probability"]),
            float(self._runtime["trigger_global_cooldown"]))
        self._arb.set_lead_role(str(self._runtime["lead_role"]))
        return dict(self._runtime)

    def snapshot(self) -> Dict[str, Any]:
        return {"enabled": self._started,
                "current_speaker": self._tt.current_speaker,
                "pending": self._tt.pending_count(),
                "runtime": dict(self._runtime),
                "recent_turns": self._tt.turn_history(limit=10)}

    def flush(self, timeout: float = 2.0) -> None:
        """测试辅助：等待异步执行排空（生产不使用）。"""
        deadline = time.time() + timeout
        while time.time() < deadline and self._tt.pending_count() > 0:
            time.sleep(0.01)

    # ---------- 内部 ----------

    def _execute(self, role: str, text: str, kind: str = "danmaku") -> None:
        """仲裁放行后的入口：记录话轮 → 释放互斥 → 执行 → 排空待发队列。"""
        if self._pipeline is None:
            logger.warning("[Collaboration] pipeline 未注入，跳过执行 role=%s", role)
            return
        self._tt.record_turn(role, kind, text=text)
        self._tt.release(role)      # 释放互斥（执行异步，先放行队列）
        self._run_utterance(role, text, kind)
        self._drain_queue()

    def _run_utterance(self, role: str, text: str, kind: str = "danmaku") -> None:
        """单条执行体（供 _execute 与排空循环复用，避免递归时序问题）。"""
        base_prompt = ""
        if self._profiles is not None:
            load = getattr(self._profiles, "load", None)
            if callable(load):
                p = load(role)
                base_prompt = getattr(p, "system_prompt", "") if p else ""
        turn_context = self._ctx.global_transcript()
        try:
            asyncio.run(self._pipeline.execute_with(
                text=text, role=role,
                system_prompt=self._ctx.build_system_prompt(
                    role, base_prompt, self._runtime["awareness_enabled"]),
                turn_context=turn_context))
        except Exception as e:
            logger.error("[Collaboration] execute_with 异常: %s", e)

    def _drain_queue(self) -> None:
        """release 后立即排空待发队列（最高优先级者先行）。"""
        nxt = self._tt.dequeue()
        while nxt is not None:
            nxt_role = nxt.get("role")
            nxt_text = nxt.get("text") or nxt.get("ref_text") or ""
            nxt_kind = nxt.get("kind", "danmaku")
            self._tt.release(nxt_role)
            self._run_utterance(nxt_role, nxt_text, nxt_kind)
            nxt = self._tt.dequeue()
