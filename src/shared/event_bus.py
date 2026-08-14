"""
EventBus 事件总线 — 模块间发布/订阅通信基座

取代模块间直接持有引用的耦合方式，提供松耦合的事件驱动通信。

用法：
    from src.shared.event_bus import EventBus, EventPriority

    bus = EventBus()
    bus.subscribe("session:started", my_handler, priority=EventPriority.NORMAL)
    bus.publish("session:started", topic="日常话题", tone="分享")
    bus.unsubscribe("session:started", my_handler)

v1.1 微调（相对旧项目 core/event_bus.py）：
1. 事件名校验：subscribe()/publish() 系列校验事件名存在于 src.shared.events
   （防止业务代码手写字符串散落；通配符 ns:* / * 仅订阅侧放行）。
2. 新增 publish_sync()：显式等待所有订阅者完成的同步发布（默认异步走 publish_async）。

# 模块内容清单（8 项契约）
1. 模块身份标识：shared · EventBus · 对外接口 subscribe()/unsubscribe()/publish()/publish_async()/publish_sync()/get_history()/current_seq()/reset()
2. 配置契约：构造参数 schema/enable_history/history_size；熔断阈值 fuse_threshold=30、冷却 fuse_cooldown=5.0
3. 输入契约：subscribe(event, handler, priority, name, once)；publish(event, **data) 事件名+关键字数据
4. 输出契约：publish 同步执行匹配处理器；publish_async 新线程异步；get_subscriber_count()/get_history()/get_latest() 查询
5. 依赖声明：time、logging、threading、traceback、typing、dataclasses、enum、src.shared.events
6. 错误定义：未注册事件名抛 ValueError；处理器异常记录不中断；高频事件熔断跳过发布
7. 生命周期方法：subscribe()/unsubscribe()/publish()/publish_async()/publish_sync()/reset()（单例）
8. 领域状态说明：_handlers 事件→处理器列表、_history 事件历史、_fuse_counters/_fuse_banned 熔断状态
"""
import time
import logging
import threading
import traceback
from typing import Callable, Dict, List, Optional, Set
from dataclasses import dataclass, field
from enum import IntEnum

from src.shared.events import ALL_EVENTS

logger = logging.getLogger(__name__)


class EventPriority(IntEnum):
    """事件优先级（高优先先执行）"""
    HIGHEST = 10
    HIGH = 8
    NORMAL = 5
    LOW = 3
    LOWEST = 1


@dataclass
class EventHandler:
    """事件处理器包装"""
    callback: Callable
    priority: int = EventPriority.NORMAL
    name: str = ""
    once: bool = False

    def __post_init__(self):
        if not self.name:
            self.name = getattr(self.callback, "__name__", str(id(self.callback)))


@dataclass
class EventRecord:
    """事件记录（用于调试/监控）"""
    event: str
    data: Dict
    handler_count: int
    timestamp: float = 0.0
    seq: int = 0  # 全局单调序号（v1.2 新增）
    handler_results: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


class EventBus:
    """事件总线（单例模式）— 线程安全的发布-订阅实现"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, schema: Optional[Set[str]] = None,
                 enable_history: bool = True, history_size: int = 200):
        if getattr(self, "_initialized", False):
            return
        self._schema = ALL_EVENTS if schema is None else schema
        self._mutex = threading.RLock()
        self._handlers: Dict[str, List[EventHandler]] = {}
        self._history: List[EventRecord] = []
        self._enable_history = enable_history
        self._history_size = history_size
        self._fuse_counters: Dict[str, tuple] = {}
        self._fuse_threshold = 30
        self._fuse_cooldown = 5.0
        self._fuse_banned: Dict[str, float] = {}
        self._seq_counter = 0  # 全局单调序号（v1.2 新增）
        self._initialized = True
        logger.info("[EventBus] 已初始化 (history_size=%d, fuse_threshold=%d)",
                    history_size, self._fuse_threshold)

    # ---------- 事件名校验（v1.1 微调 1） ----------

    def _validate(self, event: str) -> None:
        """校验事件名已注册；通配符（ns:* / *）仅订阅侧使用，放行。"""
        if event == "*" or (event.endswith(":*")):
            return
        if event not in self._schema:
            raise ValueError(
                f"[EventBus] 未注册事件名: {event}（请在 src/shared/events.py 中定义）"
            )

    def subscribe(self, event: str, handler: Callable,
                  priority: int = EventPriority.NORMAL,
                  name: str = "",
                  once: bool = False):
        """订阅事件"""
        self._validate(event)
        with self._mutex:
            if event not in self._handlers:
                self._handlers[event] = []
            eh = EventHandler(
                callback=handler,
                priority=priority,
                name=name or getattr(handler, "__name__", str(id(handler))),
                once=once
            )
            self._handlers[event].append(eh)
            self._handlers[event].sort(key=lambda h: h.priority, reverse=True)
            logger.debug("[EventBus] 订阅: %s <- %s (pri=%d)", event, eh.name, priority)

    def unsubscribe(self, event: str, handler: Optional[Callable] = None):
        """取消订阅"""
        with self._mutex:
            if event not in self._handlers:
                return
            if handler is None:
                count = len(self._handlers[event])
                del self._handlers[event]
                logger.debug("[EventBus] 取消订阅 %s: 全部 %d 个", event, count)
                return
            orig_count = len(self._handlers[event])
            self._handlers[event] = [
                h for h in self._handlers[event]
                if h.callback != handler
            ]
            removed = orig_count - len(self._handlers[event])
            if removed:
                logger.debug("[EventBus] 取消订阅: %s 已移除 %d 个", event, removed)

    def publish(self, event: str, **data):
        """发布事件（同步执行所有匹配的处理器）"""
        self._validate(event)
        if self._is_fused(event):
            logger.warning("[EventBus] 事件 %s 已被熔断，跳过发布", event)
            return
        handlers = self._resolve_handlers(event)
        if not handlers:
            # 无订阅者也要分配 seq（保持全局单调，供快照 version 使用）
            with self._mutex:
                self._seq_counter += 1
            return
        self._track_fuse(event)
        with self._mutex:
            self._seq_counter += 1
            record = EventRecord(event=event, data=data,
                                 handler_count=len(handlers),
                                 seq=self._seq_counter)
        for handler in handlers:
            try:
                handler.callback(event=event, **data)
                record.handler_results.append(f"{handler.name}: OK")
            except Exception as e:
                record.handler_results.append(f"{handler.name}: {e}")
                logger.error("[EventBus] 处理器 %s 执行异常 (事件 %s): %s",
                             handler.name, event, traceback.format_exc())
            if handler.once:
                self.unsubscribe(event, handler.callback)
        if self._enable_history:
            with self._mutex:
                self._history.append(record)
                if len(self._history) > self._history_size:
                    self._history = self._history[-self._history_size:]

    def publish_async(self, event: str, **data):
        """异步发布事件（在新线程执行，不阻塞调用方）"""
        self._validate(event)
        t = threading.Thread(
            target=self.publish, args=(event,), kwargs=data,
            daemon=True, name=f"EventBus-{event[:20]}"
        )
        t.start()

    def publish_sync(self, event: str, **data):
        """同步发布事件（显式等待所有订阅者完成）— v1.1 微调 2

        需要等待所有订阅者完成后再继续的场景使用本方法；
        其余场景默认走 publish_async（异步，不阻塞调用方）。
        """
        self.publish(event, **data)

    def get_subscriber_count(self, event: str = "") -> Dict[str, int]:
        with self._mutex:
            if event:
                return {event: len(self._handlers.get(event, []))}
            return {evt: len(hs) for evt, hs in self._handlers.items() if hs}

    def get_history(self, limit: int = 20, event_filter: str = "") -> List[EventRecord]:
        records = self._history
        if event_filter:
            records = [r for r in records if r.event == event_filter]
        return records[-limit:]

    def get_latest(self, event: str = "") -> Optional[EventRecord]:
        if not self._history:
            return None
        if event:
            for r in reversed(self._history):
                if r.event == event:
                    return r
            return None
        return self._history[-1]

    def current_seq(self) -> int:
        """当前全局事件序号（快照 version 读取用）。"""
        with self._mutex:
            return self._seq_counter

    def clear_history(self):
        with self._mutex:
            self._history.clear()

    def reset(self):
        with self._mutex:
            self._handlers.clear()
            self._history.clear()
            self._fuse_counters.clear()
            self._fuse_banned.clear()
            logger.info("[EventBus] 已重置")

    # ---------- 熔断机制 ----------

    def _track_fuse(self, event: str):
        now = time.time()
        with self._mutex:
            expired = [e for e, (c, t) in self._fuse_counters.items()
                       if now - t > 1.0]
            for e in expired:
                del self._fuse_counters[e]
            entry = self._fuse_counters.get(event)
            if entry is not None:
                count, first_time = entry
                count += 1
            else:
                count = 1
                first_time = now
            self._fuse_counters[event] = (count, first_time)
            if count > self._fuse_threshold:
                self._fuse_banned[event] = now + self._fuse_cooldown
                logger.warning("[EventBus] 事件 %s 触发熔断，冷却 %.1fs",
                               event, self._fuse_cooldown)

    def _is_fused(self, event: str) -> bool:
        now = time.time()
        ban_until = self._fuse_banned.get(event, 0)
        if now < ban_until:
            return True
        if ban_until > 0 and now >= ban_until:
            del self._fuse_banned[event]
            logger.info("[EventBus] 事件 %s 熔断已解除", event)
        return False

    def _resolve_handlers(self, event: str) -> List[EventHandler]:
        with self._mutex:
            handlers = []
            if event in self._handlers:
                handlers.extend(self._handlers[event])
            namespace = event.split(":")[0] if ":" in event else ""
            wildcard = f"{namespace}:*" if namespace else "*"
            if wildcard in self._handlers:
                handlers.extend(self._handlers[wildcard])
            if "*" in self._handlers:
                handlers.extend(self._handlers["*"])
            seen = set()
            unique = []
            for h in handlers:
                h_id = id(h.callback)
                if h_id not in seen:
                    seen.add(h_id)
                    unique.append(h)
            unique.sort(key=lambda h: h.priority, reverse=True)
            return unique


# 全局默认实例
default_bus = EventBus()
