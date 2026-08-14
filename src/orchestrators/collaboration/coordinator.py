"""coordinator.py — 多角色协作顶层协调器。

组装 arbitrator/turn_tracker/context_manager/triggers，订阅事件并驱动
执行器（pipeline）按角色执行。

执行器协议（契约）：
    async def execute_with(text, role, system_prompt, turn_context) -> dict
- text: 发言文本；role: 角色名；system_prompt: 组合后的系统提示；
  turn_context: 全局对话流（list[str]）；返回 dict。
  真实实现 DanmakuPipeline 在 Task 15 提供（发言完成时发布 speech:completed）；
  任何实现该协议的异步对象均可注入为 pipeline（参见 tests 的 FakePipeline）。

事件接线：
- danmaku:received         → _on_danmaku（未启动/命令前缀过滤）→ 仲裁 → _execute
- speech:completed         → 记录话轮（唯一记录点）+ triggers 评估 → 发布 collab:utterance_requested
- collab:utterance_requested → _on_utterance_requested → request_utterance → 回仲裁 → _execute

对外接口：start / stop / request_utterance / update_runtime / snapshot / flush。
事件回调（EventBus 订阅）：_on_danmaku / _on_speech_completed / _on_utterance_requested。

互斥语义：arbitrate 在 acquire 成功时放行并持锁至执行完成——执行在独立线程
（collab-exec，_run_in_thread）中跑完 execute_with 后 finally 释放互斥并调用
_drain_queue 排空待发队列（deferred 在此刻放行）。同一时刻仅一人发声由互斥
全程持有保证；execute_with 抛异常也不泄漏互斥。发布线程（含 asyncio 事件循环
线程）内只做启动线程，不做 asyncio.run，因此任何线程下调用都安全。
"""
import asyncio
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

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


def _run_in_thread(coro_fn: Callable[[], Any]) -> None:
    """在独立守护线程中执行协程工厂函数返回的协程。

    用法：_run_in_thread(lambda: coro(...))；线程名统一 collab-exec。
    发布线程（含 asyncio 事件循环线程）内调用都安全——asyncio.run 只会在
    新建的独立线程里创建全新事件循环，不与调用方的事件循环冲突。
    """
    threading.Thread(
        target=lambda: asyncio.run(coro_fn()),
        daemon=True,
        name="collab-exec",
    ).start()


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
        if not self._started:
            return
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
        # 话轮唯一记录点（M3）：发言完成时才记入上下文与话轮追踪
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
        """运行时调参（POST /api/collab/config 白名单）。

        白名单不含 rules_order：规则链顺序仅在构造时生效（arbitrator 组装
        规则链），运行时不可修改规则顺序；需要调整请重建协调器。
        """
        allowed = {"trigger_probability", "trigger_global_cooldown",
                   "lead_role", "awareness_enabled"}
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
        """测试辅助：等待异步执行线程排空（互斥释放且待发队列为空；生产不使用）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._tt.current_speaker is None and self._tt.pending_count() == 0:
                return
            time.sleep(0.01)

    # ---------- 内部 ----------

    def _execute(self, role: str, text: str, kind: str = "danmaku") -> None:
        """仲裁放行后的入口：启动独立执行线程。

        互斥由执行线程全程持有至 execute_with 完成（finally 释放并排空），
        此处不再提前 release；话轮记录仅发生在 _on_speech_completed（M3）。
        """
        if self._pipeline is None:
            # 未注入执行器：必须归还互斥，避免锁泄漏阻塞后续所有请求
            self._tt.release(role)
            logger.warning("[Collaboration] pipeline 未注入，跳过执行 role=%s", role)
            return
        _run_in_thread(lambda: self._run_utterance(role, text, kind))

    async def _run_utterance(self, role: str, text: str, kind: str = "danmaku") -> None:
        """执行协程：构建上下文并执行 execute_with（由 _run_in_thread 的 asyncio.run 驱动）。

        无论成功或异常，finally 中释放互斥并排空待发队列（deferred 放行）。
        """
        base_prompt = ""
        if self._profiles is not None:
            load = getattr(self._profiles, "load", None)
            if callable(load):
                p = load(role)
                base_prompt = getattr(p, "system_prompt", "") if p else ""
        turn_context = self._ctx.global_transcript()
        try:
            await self._pipeline.execute_with(
                text=text, role=role,
                system_prompt=self._ctx.build_system_prompt(
                    role, base_prompt, self._runtime["awareness_enabled"]),
                turn_context=turn_context)
        except Exception:
            logger.error("[Collaboration] execute_with 异常: role=%s text=%s",
                         role, text, exc_info=True)
        finally:
            # 互斥全程持有：完成（含异常）后释放并排空待发队列
            self._tt.release(role)
            self._drain_queue()

    def _drain_queue(self) -> None:
        """release 后排空待发队列（最高优先级者先行）。

        dequeue() 会把互斥占用给弹出的下一话轮，因此一次排空至多弹出一条；
        链式排空由新执行线程完成后的 finally（release + 再次 _drain_queue）接力，
        直到队列清空且互斥释放。
        """
        nxt = self._tt.dequeue()
        while nxt is not None:
            nxt_role = nxt.get("role")
            nxt_text = nxt.get("text") or nxt.get("ref_text") or ""
            nxt_kind = nxt.get("kind", "danmaku")
            # 默认参数绑定避免 lambda 闭包延迟绑定循环变量
            _run_in_thread(lambda r=nxt_role, t=nxt_text, k=nxt_kind:
                           self._run_utterance(r, t, k))
            nxt = self._tt.dequeue()
