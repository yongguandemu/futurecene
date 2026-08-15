"""live_intelligence_orchestrator.py — 直播间智能调度官主类（P1 精细子模块）

承载 7 个精细子模块，作为直播间智能互动决策的构建块：
  danmaku_pool / danmaku_reactor / heat_tracker / speech_queue /
  context_aggregator / commentary_policy / pace。

职责边界：
  - 弹幕入池与反应经 EventBus 自动接线（danmaku:received → pool/reactor）
  - 热度累计经 EventBus 自动接线（观众事件 → heat_tracker）
  - 情境聚合订阅关键事件维护实时缓存，供 AI 决策拉取
  - 解说与节奏为纯规则引擎，供 VN 陪看调用

# 模块内容清单（8 项契约）
1. 模块身份标识：live_intelligence 调度官 · live_intelligence_orchestrator · 能力 intel:*（弹幕池/反应/热度/发言队列/情境/解说/节奏）
2. 配置契约：commentary_style / commentary_max_words / min_commentary_interval / dialogue_quiet_seconds / pool_max_size / pool_ttl / reactor_global_cooldown / reactor_rate_max / heat_decay_per_sec / heat_event_decay / speech_max_size / speech_timeout / context_cache_ttl
3. 输入契约：handle(command) 接收 {"capability": "intel:*", "payload": {...}}；构造注入 event_bus 与各子模块
4. 输出契约：返回 {"ok": bool, "data": {...}, "error": str|null}；子模块经 EventBus 发布事件
5. 依赖声明：registry、commentary_policy、context_aggregator、danmaku_pool、danmaku_reactor、heat_tracker、pace、speech_queue
6. 错误定义：未知能力返回 {"ok": false}；ValueError/异常捕获后返回 error 字段
7. 生命周期方法：start() 启动子模块订阅、stop() 停止、health() 返回状态、handle() 能力分发
8. 领域状态说明：_started 标志 + 7 个子模块实例（弹幕池/反应器/热度/发言队列/情境聚合/解说/节奏）
"""
import logging
from typing import Any, Dict, List, Optional

from src.orchestrators.live_intelligence_orchestrator import registry
from src.orchestrators.live_intelligence_orchestrator.commentary_policy import VNCommentaryPolicy
from src.orchestrators.live_intelligence_orchestrator.context_aggregator import ContextAggregator
from src.orchestrators.live_intelligence_orchestrator.danmaku_pool import DanmakuPool
from src.orchestrators.live_intelligence_orchestrator.danmaku_reactor import DanmakuReactor
from src.orchestrators.live_intelligence_orchestrator.heat_tracker import HeatTracker
from src.orchestrators.live_intelligence_orchestrator.pace import VNPaceController
from src.orchestrators.live_intelligence_orchestrator.speech_queue import SpeechQueue
from src.shared.world_book import get_world_book

logger = logging.getLogger(__name__)


class LiveIntelligenceOrchestrator:
    """直播间智能调度官。"""

    name = "intelligence"

    def __init__(self, event_bus, config: Optional[Dict[str, Any]] = None,
                 danmaku_pool: Optional[DanmakuPool] = None,
                 danmaku_reactor: Optional[DanmakuReactor] = None,
                 heat_tracker: Optional[HeatTracker] = None,
                 speech_queue: Optional[SpeechQueue] = None,
                 context_aggregator: Optional[ContextAggregator] = None,
                 commentary_policy: Optional[VNCommentaryPolicy] = None,
                 pace: Optional[VNPaceController] = None):
        self._event_bus = event_bus
        self._config = config or {}
        self.commentary_policy = commentary_policy or VNCommentaryPolicy(
            event_bus=event_bus,
            style=self._config.get("commentary_style", "陪看吐槽型"),
            max_words=self._config.get("commentary_max_words", 40))
        self.pace = pace or VNPaceController(
            event_bus=event_bus,
            min_commentary_interval=self._config.get("min_commentary_interval", 20.0),
            dialogue_quiet_seconds=self._config.get("dialogue_quiet_seconds", 8.0))
        self.danmaku_pool = danmaku_pool or DanmakuPool(
            event_bus=event_bus,
            max_size=self._config.get("pool_max_size", 100),
            ttl=self._config.get("pool_ttl", 600.0))
        self.danmaku_reactor = danmaku_reactor or DanmakuReactor(
            event_bus=event_bus,
            global_cooldown=self._config.get("reactor_global_cooldown", 1.0),
            rate_max=self._config.get("reactor_rate_max", 20))
        self.heat_tracker = heat_tracker or HeatTracker(
            event_bus=event_bus,
            decay_per_sec=self._config.get("heat_decay_per_sec", 0.02),
            event_decay=self._config.get("heat_event_decay", 0.95))
        self.speech_queue = speech_queue or SpeechQueue(
            event_bus=event_bus,
            max_size=self._config.get("speech_max_size", 200),
            timeout=self._config.get("speech_timeout", 120.0))
        self.context_aggregator = context_aggregator or ContextAggregator(
            event_bus=event_bus,
            cache_ttl=self._config.get("context_cache_ttl", 0.5),
            danmaku_pool=self.danmaku_pool,
            heat_tracker=self.heat_tracker,
            world_book=get_world_book())  # 世界书条目注入情境聚合（world_book_entries 槽位）
        self._started = False
        registry.bind(self.handle)

    def capabilities(self) -> List[str]:
        return registry.capabilities()

    def start(self) -> None:
        if self._started:
            return
        self.danmaku_pool.start()
        self.danmaku_reactor.start()
        self.heat_tracker.start()
        self.context_aggregator.start()
        self._started = True
        logger.info("[LiveIntelligenceOrchestrator] 已启动（弹幕池/反应器/热度/情境聚合）")

    def stop(self) -> None:
        self.context_aggregator.stop()
        self.heat_tracker.stop()
        self.danmaku_reactor.stop()
        self.danmaku_pool.stop()
        self._started = False

    async def handle(self, command: Dict[str, Any]) -> Dict[str, Any]:
        capability = command.get("capability", "")
        payload = command.get("payload", {})
        name = capability.removeprefix("intel:")
        handler = getattr(self, f"_h_{name}", None)
        if handler is None:
            return {"ok": False, "data": {}, "error": f"unknown capability: {capability}"}
        try:
            return handler(payload)
        except ValueError as e:
            return {"ok": False, "data": {}, "error": str(e)}
        except Exception as e:
            logger.exception("[LiveIntelligence] 能力 %s 执行异常", capability)
            return {"ok": False, "data": {}, "error": str(e)}

    def health(self) -> Dict[str, Any]:
        return {"status": "ok" if self._started else "down",
                "detail": f"pool={self.danmaku_pool.size()}, heat={self.heat_tracker.get_score():.1f}"}

    # ---------- 内部：输出契约 ----------

    def _ok(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, "data": data, "error": None}

    # ---------- 弹幕池 ----------

    def _h_danmaku_pool_add(self, p) -> Dict[str, Any]:
        dm = self.danmaku_pool.add(p.get("text", ""), user=p.get("user", ""),
                                   platform=p.get("platform", "bilibili"))
        return self._ok({"danmaku": dm})

    def _h_danmaku_pool_pending(self, p) -> Dict[str, Any]:
        return self._ok({"pending": self.danmaku_pool.get_pending(p.get("limit", 10))})

    def _h_danmaku_pool_mark(self, p) -> Dict[str, Any]:
        marked = self.danmaku_pool.mark_processed(p.get("danmaku_ids", []))
        return self._ok({"marked": marked})

    def _h_danmaku_pool_clear(self, p) -> Dict[str, Any]:
        return self._ok({"cleared": self.danmaku_pool.clear()})

    def _h_danmaku_pool_stats(self, p) -> Dict[str, Any]:
        return self._ok(self.danmaku_pool.get_stats())

    # ---------- 弹幕反应器 ----------

    def _h_react(self, p) -> Dict[str, Any]:
        reply = self.danmaku_reactor.react(p.get("text", ""), user=p.get("user", ""))
        return self._ok({"reply": reply, "sentiment": self._last_sentiment(p)})

    def _last_sentiment(self, p) -> str:
        # 反应器不暴露单次情感，用最近历史推断
        try:
            history = getattr(self.danmaku_reactor, "_history", [])
            if history:
                return history[-1].get("sentiment", "neutral")
        except Exception:
            pass
        return "neutral"

    def _h_react_register(self, p) -> Dict[str, Any]:
        self.danmaku_reactor.register_reaction(p.get("sentiment", "neutral"),
                                               p.get("templates", []))
        return self._ok({"registered": p.get("sentiment", "neutral")})

    def _h_react_stats(self, p) -> Dict[str, Any]:
        return self._ok(self.danmaku_reactor.get_stats())

    # ---------- 热度追踪 ----------

    def _h_heat_record(self, p) -> Dict[str, Any]:
        score = self.heat_tracker.record_event(p.get("event_type", ""),
                                               weight=p.get("weight"),
                                               count=p.get("count", 1))
        return self._ok({"score": score, "level": self.heat_tracker.get_level()})

    def _h_heat_score(self, p) -> Dict[str, Any]:
        return self._ok({"score": self.heat_tracker.get_score(),
                         "level": self.heat_tracker.get_level()})

    def _h_heat_level(self, p) -> Dict[str, Any]:
        return self._ok({"level": self.heat_tracker.get_level()})

    def _h_heat_trend(self, p) -> Dict[str, Any]:
        return self._ok({"trend": self.heat_tracker.get_trend(p.get("window", 60.0))})

    def _h_heat_stats(self, p) -> Dict[str, Any]:
        return self._ok(self.heat_tracker.get_stats())

    def _h_heat_history(self, p) -> Dict[str, Any]:
        return self._ok({"history": self.heat_tracker.get_history(p.get("limit", 20))})

    # ---------- 发言队列 ----------

    def _h_speech_enqueue(self, p) -> Dict[str, Any]:
        ok = self.speech_queue.enqueue(p.get("char_id", ""), p.get("text", ""),
                                       priority=p.get("priority", 0))
        return self._ok({"queued": ok})

    def _h_speech_dequeue(self, p) -> Dict[str, Any]:
        return self._ok({"items": self.speech_queue.dequeue(p.get("limit", 1))})

    def _h_speech_peek(self, p) -> Dict[str, Any]:
        return self._ok({"items": self.speech_queue.peek(p.get("limit", 5))})

    def _h_speech_clear(self, p) -> Dict[str, Any]:
        return self._ok({"cleared": self.speech_queue.clear()})

    def _h_speech_stats(self, p) -> Dict[str, Any]:
        return self._ok(self.speech_queue.get_stats())

    # ---------- 情境聚合 ----------

    def _h_context_snapshot(self, p) -> Dict[str, Any]:
        snap = self.context_aggregator.get_snapshot(role=p.get("role", "yuki"),
                                                    focus=p.get("focus", "role"))
        return self._ok({"snapshot": self._snap_to_dict(snap)})

    def _h_context_role(self, p) -> Dict[str, Any]:
        return self._ok(self.context_aggregator.get_role_context(role=p.get("role", "yuki")))

    def _h_context_ops(self, p) -> Dict[str, Any]:
        return self._ok(self.context_aggregator.get_ops_context())

    def _h_context_stats(self, p) -> Dict[str, Any]:
        return self._ok(self.context_aggregator.get_status())

    # ---------- VN 解说 ----------

    def _h_commentary_generate(self, p) -> Dict[str, Any]:
        commentary = self.commentary_policy.generate_commentary(
            p.get("state", ""), text=p.get("text", ""))
        self.commentary_policy.publish_generated(p.get("state", ""), commentary)
        return self._ok({"commentary": commentary, "state": p.get("state", "")})

    def _h_commentary_set_style(self, p) -> Dict[str, Any]:
        self.commentary_policy.style = p.get("style", self.commentary_policy.style)
        return self._ok({"style": self.commentary_policy.style})

    def _h_pace_decide(self, p) -> Dict[str, Any]:
        decision = self.pace.decide_state(p.get("state", ""), text=p.get("text", ""),
                                          now=p.get("now"))
        return self._ok({"should_comment": decision.should_comment,
                         "reason": decision.reason, "priority": decision.priority})

    # ---------- 辅助 ----------

    def _snap_to_dict(self, snap) -> Dict[str, Any]:
        return {
            "recent_danmaku": snap.recent_danmaku,
            "current_emotion": snap.current_emotion,
            "host_mood": snap.host_mood,
            "current_schedule": snap.current_schedule,
            "relevant_memories": snap.relevant_memories,
            "world_book_entries": snap.world_book_entries,
            "screen_understanding": snap.screen_understanding,
            "active_tools": snap.active_tools,
            "session_stats": snap.session_stats,
            "game_state": snap.game_state,
            "commentary_status": snap.commentary_status,
            "heat_status": snap.heat_status,
            "module_health": snap.module_health,
            "bilibili_connected": snap.bilibili_connected,
            "live_status": snap.live_status,
            "api_cost_today": snap.api_cost_today,
            "timestamp": snap.timestamp,
            "token_estimate": snap.token_estimate,
        }