"""模块内容清单 — context_aggregator

## 1. 模块身份标识
- 所属调度官：live_intelligence_orchestrator
- 能力名：intel:context_snapshot / context_role / context_ops / context_stats
- 引擎名：（单实现）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| cache_ttl | 否 | 0.5 | float 秒 | 快照缓存 TTL，目标 ≤ 500ms |
| token_budget_total | 否 | 8000 | int | 总 token 预算 |
| max_recent_danmaku | 否 | 30 | int | 实时缓存弹幕条数 |

## 3. 输入契约
- intel:context_snapshot 输入：{"role"?: str, "focus"?: str}
  - role 可选，str，默认 "yuki"
  - focus 可选，str ∈ {role, operations, full}，默认 "role"
- context_role / context_ops / context_stats 输入：无

## 4. 输出契约
- 成功：{"ok": true, "data": {"snapshot": {...}}, "error": null}
- 失败：{"ok": false, "data": {}, "error": str}

## 5. 依赖声明
- 外部服务：无（psutil 可选，缺失时资源指标返回 0）
- 内部模块：shared/events、shared/event_bus（必需）；注入的各模块提供者（防御式，可为 None）
- 预先配置：无

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| ValueError | focus 非法 | 调用方校验输入 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| init | 是 | 初始化实时缓存、模块注册表、快照缓存 |
| start | 是 | 订阅关键事件维护实时缓存 |
| stop | 是 | 取消全部订阅 |
| health | 是 | 返回缓存命中率与构建耗时 |

## 8. 领域状态说明
- 状态项：_danmaku_cache/_gift_cache/_interaction_cache（实时缓存）、_snapshot_cache、_live_status
- 持久化：无
- 恢复：无（start 重建订阅与缓存）
"""
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

from src.shared.events import CONTEXT_SNAPSHOT_READY

logger = logging.getLogger(__name__)

# ========== Token 预算配置 ==========
TOKEN_BUDGET_TOTAL = 8000
TOKEN_BUDGET_ROLE = {
    "danmaku": 1500, "memory": 2000, "world_book": 2000,
    "screen": 500, "session_stats": 200,
}
TOKEN_BUDGET_ROLE_TOTAL = sum(TOKEN_BUDGET_ROLE.values())  # 6200
TOKEN_BUDGET_OPS = {"module_health": 800, "cost": 200, "community": 200}
TOKEN_BUDGET_OPS_TOTAL = sum(TOKEN_BUDGET_OPS.values())  # 1200

CACHE_TTL = 0.5
MAX_RECENT_DANMAKU = 30
MAX_RECENT_GIFTS = 10
MAX_RECENT_INTERACTIONS = 20
EVENT_TTL = 300


@dataclass
class ContextSnapshot:
    """情境快照数据结构（角色维度 + 运营维度 + 元数据）。"""

    # === 角色维度（供角色 Agent 使用）===
    recent_danmaku: List[dict] = field(default_factory=list)
    current_emotion: str = "calm"
    host_mood: str = ""
    current_schedule: dict = field(default_factory=dict)
    relevant_memories: List[dict] = field(default_factory=list)
    world_book_entries: List[dict] = field(default_factory=list)
    screen_understanding: str = ""
    active_tools: List[str] = field(default_factory=list)
    session_stats: dict = field(default_factory=dict)

    # === 二期模块状态摘要（供角色 Agent 决策参考）===
    game_state: dict = field(default_factory=dict)
    commentary_status: dict = field(default_factory=dict)
    heat_status: dict = field(default_factory=dict)

    # === 运营维度（供运营 Agent 使用）===
    module_health: dict = field(default_factory=dict)
    module_details: dict = field(default_factory=dict)
    bilibili_connected: bool = False
    tts_queue_length: int = 0
    llm_last_latency: float = 0.0
    memory_usage_pct: float = 0.0
    cpu_usage_pct: float = 0.0
    degraded_modules: List[str] = field(default_factory=list)
    live_status: str = "offline"
    api_cost_today: float = 0.0
    community_pending: int = 0

    # === 元数据 ===
    timestamp: float = 0.0
    snapshot_build_time: float = 0.0
    token_estimate: int = 0


class ContextAggregator:
    """情境聚合器 — 把分散信息源聚合成 ContextSnapshot 供 AI 决策。

    信息获取方式（互补）：
      - 推送式：EventBus 订阅关键事件维护实时缓存
      - 拉取式：从注入的模块提供者拉取状态快照（防御式，模块可为 None）
    """

    def __init__(self, event_bus=None, **kwargs):
        self._event_bus = event_bus
        self._mood_state = None
        self._cache_ttl = float(kwargs.get("cache_ttl", CACHE_TTL))

        # ---- 模块引用（防御式，可为 None）----
        self.danmaku_pool = kwargs.get("danmaku_pool")
        self.memory_manager = kwargs.get("memory_manager")
        self.world_book = kwargs.get("world_book")
        self.schedule_engine = kwargs.get("schedule_engine")
        self.screen_perception = kwargs.get("screen_perception")
        self.brain = kwargs.get("brain")
        self.tts_client = kwargs.get("tts_client")
        self.bilibili_connector = kwargs.get("bilibili_connector")
        self.api_cost_tracker = kwargs.get("api_cost_tracker")
        self.tool_registry = kwargs.get("tool_registry")
        self.game_ai = kwargs.get("game_ai")
        self.commentary_rhythm = kwargs.get("commentary_rhythm")
        self.heat_tracker = kwargs.get("heat_tracker")

        # ---- 模块注册表（统一拉取）----
        self._module_registry: Dict[str, Any] = {}
        for name, mod in (
            ("brain", self.brain), ("tts", self.tts_client),
            ("bilibili", self.bilibili_connector), ("danmaku_pool", self.danmaku_pool),
            ("memory", self.memory_manager), ("schedule", self.schedule_engine),
            ("world_book", self.world_book), ("screen_perception", self.screen_perception),
            ("api_cost", self.api_cost_tracker), ("tool_registry", self.tool_registry),
            ("game_ai", self.game_ai), ("commentary_rhythm", self.commentary_rhythm),
            ("heat_tracker", self.heat_tracker),
        ):
            if mod is not None:
                self._module_registry[name] = mod

        # ---- 实时事件缓存 ----
        self._danmaku_cache: Deque[dict] = deque(maxlen=MAX_RECENT_DANMAKU)
        self._gift_cache: Deque[dict] = deque(maxlen=MAX_RECENT_GIFTS)
        self._interaction_cache: Deque[dict] = deque(maxlen=MAX_RECENT_INTERACTIONS)
        self._live_status: str = "offline"
        self._session_start_time: Optional[float] = None
        self._bilibili_connected: bool = False
        self._degraded_alerts: Dict[str, float] = {}

        # ---- 快照缓存 ----
        self._snapshot_cache: Dict[tuple, ContextSnapshot] = {}
        self._cache_lock = threading.Lock()
        self._cache_lock_realtime = threading.Lock()

        self._snapshot_count = 0
        self._cache_hit_count = 0
        self._last_build_time = 0.0
        self._started = False
        self._subscriptions: List[tuple] = []
        logger.info("[ContextAggregator] 初始化完成，已注册 %d 个模块",
                    len(self._module_registry))

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        if self._event_bus is None:
            logger.warning("[ContextAggregator] EventBus 不可用，仅拉取模式运行")
            return
        subs = [
            ("danmaku:received", self._on_danmaku),
            ("gift:received", self._on_gift),
            ("audience:entered", self._on_interaction),
            ("bilibili:connected", lambda e="", **k: self._set_bili(True)),
            ("bilibili:disconnected", lambda e="", **k: self._set_bili(False)),
            ("session:started", self._on_session_started),
            ("session:ended", self._on_session_ended),
            ("safety:blocked", self._on_safety_alert),
            ("cost:circuit_open", self._on_cost_alert),
            ("module:degraded", self._on_module_degraded),
        ]
        for event, handler in subs:
            try:
                self._event_bus.subscribe(event, handler)
                self._subscriptions.append((event, handler))
            except Exception as e:
                logger.debug("[ContextAggregator] 订阅 %s 失败: %s", event, e)
        logger.info("[ContextAggregator] 已订阅 %d 个事件", len(self._subscriptions))

    def stop(self) -> None:
        self._started = False
        if self._event_bus is None:
            return
        for event, handler in self._subscriptions:
            try:
                self._event_bus.unsubscribe(event, handler)
            except Exception as e:
                logger.warning("[ContextAggregator] 取消订阅 %s 失败: %s", event, e)
        self._subscriptions.clear()
        logger.info("[ContextAggregator] 已停止")

    def health(self) -> Dict[str, Any]:
        return {"status": "ok" if self._started else "degraded",
                "detail": f"snapshots={self._snapshot_count}, hits={self._cache_hit_count}"}

    # ---------- 事件回调（维护实时缓存） ----------

    def _set_bili(self, connected: bool) -> None:
        self._bilibili_connected = connected

    def _on_danmaku(self, event: str = "", **data) -> None:
        try:
            entry = {
                "uid": data.get("uid", ""),
                "name": data.get("user_name") or data.get("username") or data.get("name", ""),
                "content": data.get("content") or data.get("text", ""),
                "timestamp": data.get("timestamp", time.time()),
                "source": data.get("source", ""),
                "type": data.get("type", "danmaku"),
            }
            with self._cache_lock_realtime:
                self._danmaku_cache.append(entry)
        except Exception as e:
            logger.debug("[ContextAggregator] 弹幕缓存失败: %s", e)

    def _on_gift(self, event: str = "", **data) -> None:
        try:
            entry = {
                "sender": data.get("username") or data.get("uname", ""),
                "gift_name": data.get("gift_name", ""),
                "num": data.get("num", 1),
                "total_price": data.get("total_price", 0),
                "timestamp": data.get("timestamp", time.time()),
            }
            with self._cache_lock_realtime:
                self._gift_cache.append(entry)
        except Exception as e:
            logger.debug("[ContextAggregator] 礼物缓存失败: %s", e)

    def _on_interaction(self, event: str = "", **data) -> None:
        try:
            entry = {
                "type": data.get("type", "enter"),
                "username": data.get("username") or data.get("uname", ""),
                "timestamp": data.get("timestamp", time.time()),
            }
            with self._cache_lock_realtime:
                self._interaction_cache.append(entry)
        except Exception as e:
            logger.debug("[ContextAggregator] 互动缓存失败: %s", e)

    def _on_session_started(self, event: str = "", **data) -> None:
        self._live_status = "live"
        self._session_start_time = data.get("timestamp", time.time())

    def _on_session_ended(self, event: str = "", **data) -> None:
        self._live_status = "post_live"

    def _on_safety_alert(self, event: str = "", **data) -> None:
        self._degraded_alerts["safety"] = time.time()

    def _on_cost_alert(self, event: str = "", **data) -> None:
        level = data.get("level", "yellow")
        if level in ("red", "yellow"):
            self._degraded_alerts["api_cost"] = time.time()

    def _on_module_degraded(self, event: str = "", **data) -> None:
        module = data.get("module", "unknown")
        self._degraded_alerts[module] = time.time()

    # ---------- 公开接口 ----------

    def set_mood_state(self, mood_state) -> None:
        self._mood_state = mood_state

    def get_snapshot(self, role: str = "yuki", focus: str = "role") -> ContextSnapshot:
        if focus not in ("role", "operations", "full"):
            raise ValueError("focus must be one of role/operations/full")
        cache_key = (role, focus)
        now = time.time()
        with self._cache_lock:
            cached = self._snapshot_cache.get(cache_key)
            if cached and (now - cached.timestamp) < self._cache_ttl:
                self._cache_hit_count += 1
                return cached
        snapshot = self._build_snapshot(role, focus)
        with self._cache_lock:
            self._snapshot_cache[cache_key] = snapshot
        self._snapshot_count += 1
        if self._event_bus:
            try:
                self._event_bus.publish(CONTEXT_SNAPSHOT_READY, role=role, focus=focus,
                                        token_estimate=snapshot.token_estimate)
            except Exception as e:
                logger.debug("[ContextAggregator] 发布快照事件失败: %s", e)
        return snapshot

    def get_role_context(self, role: str = "yuki") -> dict:
        snap = self.get_snapshot(role=role, focus="role")
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
        }

    def get_ops_context(self) -> dict:
        snap = self.get_snapshot(focus="operations")
        return {
            "module_health": snap.module_health,
            "module_details": snap.module_details,
            "bilibili_connected": snap.bilibili_connected,
            "tts_queue_length": snap.tts_queue_length,
            "llm_last_latency": snap.llm_last_latency,
            "memory_usage_pct": snap.memory_usage_pct,
            "cpu_usage_pct": snap.cpu_usage_pct,
            "degraded_modules": snap.degraded_modules,
            "live_status": snap.live_status,
            "api_cost_today": snap.api_cost_today,
            "community_pending": snap.community_pending,
        }

    def get_status(self) -> dict:
        return {
            "module_name": "ContextAggregator",
            "status": "healthy" if self._started else "degraded",
            "started": self._started,
            "registered_modules": len(self._module_registry),
            "module_names": list(self._module_registry.keys()),
            "cache_ttl": self._cache_ttl,
            "snapshot_count": self._snapshot_count,
            "cache_hit_count": self._cache_hit_count,
            "last_build_time_ms": round(self._last_build_time * 1000, 1),
            "token_budget_total": TOKEN_BUDGET_TOTAL,
            "live_status": self._live_status,
            "realtime_cache": {
                "danmaku": len(self._danmaku_cache),
                "gifts": len(self._gift_cache),
                "interactions": len(self._interaction_cache),
            },
        }

    # ---------- 快照构建 ----------

    def _build_snapshot(self, role: str, focus: str) -> ContextSnapshot:
        start = time.time()
        snap = ContextSnapshot()
        snap.timestamp = start
        need_role = focus in ("role", "full")
        need_ops = focus in ("operations", "full")

        if need_role:
            snap.recent_danmaku = self._collect_recent_danmaku()
            snap.current_emotion = self._collect_current_emotion(role)
            snap.host_mood = self._collect_host_mood()
            snap.current_schedule = self._collect_current_schedule(role)
            snap.relevant_memories = self._collect_relevant_memories()
            snap.world_book_entries = self._collect_world_book_entries()
            snap.screen_understanding = self._collect_screen_understanding()
            snap.active_tools = self._collect_active_tools()
            snap.session_stats = self._collect_session_stats()
            snap.game_state = self._collect_game_state()
            snap.commentary_status = self._collect_commentary_status()
            snap.heat_status = self._collect_heat_status()
            snap.recent_danmaku = self._truncate_danmaku(
                snap.recent_danmaku, TOKEN_BUDGET_ROLE["danmaku"])
            snap.relevant_memories = self._truncate_memories(
                snap.relevant_memories, TOKEN_BUDGET_ROLE["memory"])
            snap.world_book_entries = self._truncate_world_book(
                snap.world_book_entries, TOKEN_BUDGET_ROLE["world_book"])
            snap.screen_understanding = self._truncate_text(
                snap.screen_understanding, TOKEN_BUDGET_ROLE["screen"])

        if need_ops:
            snap.module_health, snap.module_details = self._collect_module_health()
            snap.bilibili_connected = self._collect_bilibili_connected()
            snap.tts_queue_length = self._collect_tts_queue_length()
            snap.llm_last_latency = self._collect_llm_latency()
            snap.memory_usage_pct, snap.cpu_usage_pct = self._collect_system_resources()
            snap.degraded_modules = self._collect_degraded_modules(snap.module_health)
            snap.live_status = self._collect_live_status()
            snap.api_cost_today = self._collect_api_cost()
            snap.community_pending = self._collect_community_pending()

        snap.snapshot_build_time = time.time() - start
        self._last_build_time = snap.snapshot_build_time
        snap.token_estimate = self._estimate_tokens(snap)
        if snap.snapshot_build_time > 0.5:
            logger.warning("[ContextAggregator] 快照构建耗时 %.0fms 超过 500ms 目标",
                           snap.snapshot_build_time * 1000)
        return snap

    # ---------- 角色维度收集 ----------

    def _collect_recent_danmaku(self) -> List[dict]:
        result: List[dict] = []
        seen = set()
        with self._cache_lock_realtime:
            for dm in reversed(self._danmaku_cache):
                key = str(dm.get("content", ""))
                if key not in seen:
                    seen.add(key)
                    result.append(dict(dm))
                if len(result) >= MAX_RECENT_DANMAKU:
                    break
        pool = self.danmaku_pool
        if pool is not None:
            try:
                for dm in reversed(pool.get_pending(MAX_RECENT_DANMAKU)):
                    if len(result) >= MAX_RECENT_DANMAKU:
                        break
                    entry = {
                        "uid": dm.get("id", ""),
                        "name": dm.get("user", ""),
                        "content": dm.get("text", ""),
                        "timestamp": dm.get("timestamp", 0),
                        "source": dm.get("platform", "bilibili"),
                    }
                    key = str(entry["content"])
                    if key not in seen:
                        seen.add(key)
                        result.append(entry)
            except Exception as e:
                logger.debug("[ContextAggregator] 弹幕池读取失败: %s", e)
        result.sort(key=lambda d: d.get("timestamp", 0), reverse=True)
        return result[:MAX_RECENT_DANMAKU]

    def _collect_current_emotion(self, role: str) -> str:
        if self.brain is not None:
            anim = getattr(self.brain, "animation_controller", None)
            if anim is not None:
                try:
                    mood = getattr(anim, "current_mood", None)
                    if mood is None and hasattr(anim, "get_current_mood"):
                        mood = anim.get_current_mood()
                    if mood:
                        return str(mood)
                except Exception as e:
                    logger.warning("[ContextAggregator] 获取 %s 情绪失败: %s", role, e)
        return "calm"

    def _collect_host_mood(self) -> str:
        ms = self._mood_state
        if ms is not None and hasattr(ms, "get_mood_text"):
            try:
                return ms.get_mood_text()
            except Exception as e:
                logger.debug("[ContextAggregator] 心情状态获取失败: %s", e)
        return ""

    def _collect_current_schedule(self, role: str = "") -> dict:
        engine = self.schedule_engine
        if engine is None:
            return {}
        try:
            if hasattr(engine, "get_active_entry"):
                entry = engine.get_active_entry(role or None)
                if entry:
                    return {
                        "type": entry.get("type", ""), "role": entry.get("role", role),
                        "title": entry.get("title", ""),
                        "start": entry.get("start_time", "") or entry.get("start", ""),
                        "end": entry.get("end_time", "") or entry.get("end", ""),
                        "theme": entry.get("theme", ""), "mood_tone": entry.get("mood_tone", ""),
                        "constraints": entry.get("constraints", ""),
                    }
            if hasattr(engine, "get_today_schedule"):
                today = engine.get_today_schedule()
                entries = today.get("entries", []) if isinstance(today, dict) else []
                return {"today_entries": len(entries)}
        except Exception as e:
            logger.debug("[ContextAggregator] 日程读取失败: %s", e)
        return {}

    def _collect_relevant_memories(self) -> List[dict]:
        mem = self.memory_manager
        if mem is None:
            return []
        result: List[dict] = []
        try:
            l2 = getattr(mem, "l2_cache", None)
            if l2:
                for e in reversed(l2[-5:]):
                    result.append({"summary": e.get("summary", ""),
                                   "timestamp": e.get("timestamp", 0),
                                   "persistent": e.get("persistent", False)})
                return result
            l1 = getattr(mem, "l1_cache", None)
            if l1:
                for e in reversed(l1[-5:]):
                    result.append({"summary": e.get("summary", ""),
                                   "timestamp": e.get("timestamp", 0)})
        except Exception as e:
            logger.debug("[ContextAggregator] 记忆读取失败: %s", e)
        return result

    def _collect_world_book_entries(self) -> List[dict]:
        wb = self.world_book
        if wb is None and self.brain is not None:
            wb = getattr(self.brain, "world_book", None)
        if wb is None:
            return []
        entries: List[dict] = []
        try:
            if hasattr(wb, "get_enabled_entries"):
                for entry in wb.get_enabled_entries()[:15]:
                    entries.append(entry if isinstance(entry, dict) else {"name": str(entry)})
            elif hasattr(wb, "get_entries_by_category"):
                for cat in ("character", "setting", "always_on"):
                    try:
                        for entry in wb.get_entries_by_category(cat)[:5]:
                            entries.append(entry if isinstance(entry, dict) else {"name": str(entry)})
                    except Exception as e:
                        logger.warning("[ContextAggregator] 世界书 %s 读取失败: %s", cat, e)
        except Exception as e:
            logger.debug("[ContextAggregator] 世界书读取失败: %s", e)
        return entries

    def _collect_screen_understanding(self) -> str:
        sp = self.screen_perception
        if sp is not None:
            for method_name in ("get_perception", "get_latest_description",
                                "get_current_description", "get_description", "describe"):
                method = getattr(sp, method_name, None)
                if callable(method):
                    try:
                        result = method()
                        if result:
                            return str(result)
                    except Exception:
                        continue
        return ""

    def _collect_active_tools(self) -> List[str]:
        tr = self.tool_registry
        if tr is None:
            return []
        try:
            if hasattr(tr, "list_names"):
                return list(tr.list_names())
            if hasattr(tr, "list_tools"):
                return [t.get("name", str(t)) if isinstance(t, dict) else str(t)
                        for t in tr.list_tools()]
        except Exception as e:
            logger.debug("[ContextAggregator] 工具列表读取失败: %s", e)
        return []

    def _collect_session_stats(self) -> dict:
        stats: Dict[str, Any] = {}
        if self._session_start_time:
            stats["session_duration"] = int(time.time() - self._session_start_time)
        with self._cache_lock_realtime:
            stats["recent_danmaku_count"] = len(self._danmaku_cache)
            stats["recent_gift_count"] = len(self._gift_cache)
            stats["recent_interaction_count"] = len(self._interaction_cache)
        if self.danmaku_pool is not None:
            try:
                pool_stats = self.danmaku_pool.get_stats()
                stats["pool_size"] = pool_stats.get("size", 0)
                stats["pool_pending"] = pool_stats.get("pending", 0)
            except Exception as e:
                logger.debug("[ContextAggregator] 弹幕池统计失败: %s", e)
        return stats

    def _collect_game_state(self) -> dict:
        game_ai = self._module_registry.get("game_ai")
        if game_ai is None:
            return {}
        st = self._pull_status(game_ai)
        if isinstance(st, dict):
            return {k: st.get(k) for k in
                    ("running", "last_danger_level", "adapter", "uptime")}
        return {}

    def _collect_commentary_status(self) -> dict:
        cr = self._module_registry.get("commentary_rhythm")
        if cr is None:
            return {}
        st = self._pull_status(cr)
        if isinstance(st, dict):
            return {k: st.get(k) for k in
                    ("current_layer", "current_role", "queue_size", "running")}
        return {}

    def _collect_heat_status(self) -> dict:
        ht = self._module_registry.get("heat_tracker")
        if ht is None:
            return {}
        try:
            if hasattr(ht, "get_stats"):
                return ht.get_stats()
            st = self._pull_status(ht)
            if isinstance(st, dict):
                return st
        except Exception as e:
            logger.debug("[ContextAggregator] 热度状态读取失败: %s", e)
        return {}

    # ---------- 运营维度收集 ----------

    def _collect_module_health(self) -> tuple:
        health: Dict[str, str] = {}
        details: Dict[str, dict] = {}
        for name, mod in self._module_registry.items():
            try:
                status = self._pull_status(mod)
                if status is None:
                    health[name] = "down"
                    continue
                state = self._classify_health(status)
                health[name] = state
            except Exception as e:
                logger.debug("[ContextAggregator] 模块 %s 状态拉取失败: %s", name, e)
                health[name] = "down"
        return health, details

    def _collect_bilibili_connected(self) -> bool:
        if self._bilibili_connected:
            return True
        conn = self.bilibili_connector
        if conn is not None:
            try:
                if hasattr(conn, "is_connected"):
                    return bool(conn.is_connected())
                status = self._pull_status(conn)
                if isinstance(status, dict):
                    return bool(status.get("connected", False))
            except Exception as e:
                logger.warning("[ContextAggregator] 获取 B站连接状态失败: %s", e)
        return False

    def _collect_tts_queue_length(self) -> int:
        tts = self.tts_client
        if tts is None:
            return 0
        try:
            status = self._pull_status(tts)
            if isinstance(status, dict):
                return int(status.get("queue_length", 0))
        except Exception as e:
            logger.warning("[ContextAggregator] 获取 TTS 队列长度失败: %s", e)
        return 0

    def _collect_llm_latency(self) -> float:
        if self.brain is not None:
            try:
                status = self._pull_status(self.brain)
                if isinstance(status, dict) and status.get("last_latency"):
                    return float(status["last_latency"])
            except Exception as e:
                logger.warning("[ContextAggregator] 获取 LLM 延迟失败: %s", e)
        return 0.0

    def _collect_system_resources(self) -> tuple:
        try:
            import psutil
            return float(psutil.virtual_memory().percent), float(psutil.cpu_percent(interval=None))
        except Exception:
            return 0.0, 0.0

    def _collect_degraded_modules(self, module_health: dict) -> List[str]:
        degraded = [name for name, state in module_health.items()
                    if state in ("degraded", "down")]
        now = time.time()
        for module, ts in list(self._degraded_alerts.items()):
            if now - ts < EVENT_TTL and module not in degraded:
                degraded.append(module)
        return degraded

    def _collect_live_status(self) -> str:
        if self._live_status in ("live", "post_live"):
            return self._live_status
        if self._collect_bilibili_connected():
            return "live"
        return "offline"

    def _collect_api_cost(self) -> float:
        tracker = self.api_cost_tracker
        if tracker is None:
            return 0.0
        try:
            if hasattr(tracker, "get_total_cost"):
                return float(tracker.get_total_cost(24) or 0.0)
            status = self._pull_status(tracker)
            if isinstance(status, dict):
                return float(status.get("session_cost", 0.0))
        except Exception as e:
            logger.debug("[ContextAggregator] API 成本读取失败: %s", e)
        return 0.0

    def _collect_community_pending(self) -> int:
        return 0

    # ---------- 辅助 ----------

    def _pull_status(self, mod) -> Optional[dict]:
        if mod is None:
            return None
        for method_name in ("get_status", "get_state_snapshot", "get_session_status",
                            "get_current_scene", "get_state", "health"):
            getter = getattr(mod, method_name, None)
            if not callable(getter):
                continue
            try:
                result = getter()
            except Exception:
                continue
            if result is None:
                continue
            if isinstance(result, dict):
                return result
            return {"value": str(result), "source_method": method_name}
        return None

    def _classify_health(self, status: dict) -> str:
        state = status.get("status", "")
        if state in ("down", "error"):
            return "down"
        if state in ("degraded", "warning"):
            return "degraded"
        return "healthy"

    def _truncate_danmaku(self, items, budget: int) -> List[dict]:
        return items[: max(1, budget // 50)]

    def _truncate_memories(self, items, budget: int) -> List[dict]:
        return items[: max(1, budget // 100)]

    def _truncate_world_book(self, items, budget: int) -> List[dict]:
        return items[: max(1, budget // 200)]

    def _truncate_text(self, text: str, budget: int) -> str:
        if not text:
            return ""
        return text[: int(budget * 1.5)]

    def _estimate_tokens(self, snap: ContextSnapshot) -> int:
        total = 0
        total += sum(len(str(d.get("content", ""))) for d in snap.recent_danmaku)
        total += sum(len(str(m.get("summary", ""))) for m in snap.relevant_memories)
        total += sum(len(str(w.get("name", ""))) for w in snap.world_book_entries)
        total += len(snap.screen_understanding)
        total += len(str(snap.current_schedule))
        return total // 2