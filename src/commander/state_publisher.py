"""state_publisher.py — 状态变更 → state:changed 快照发布（前端重构 · 方案 A）

订阅多类触发事件，收到后生成全量快照并发布 state:changed：
1. switch:changed（开关切换）
2. session:switched / session:state_changed（角色/会话变更）
3. character:presence_changed / speech:arbitrated / speech:completed（多角色在场/发言）
4. degradation 变更（降级管理器）
5. watchdog 状态翻转（ok↔degraded↔down）
6. cost:circuit_open / cost:milestone（成本熔断触发 / 跨整元触发）

# 模块内容清单（8 项契约）
1. 模块身份标识：commander · StatePublisher · 对外 start()/stop()
2. 配置契约：构造注入 event_bus/state_provider
3. 输入契约：订阅事件回调（event, **data）
4. 输出契约：发布 STATE_CHANGED 事件，data={"snapshot": 全量快照}
5. 依赖声明：logging、src.shared.event_bus、src.shared.events
6. 错误定义：快照生成异常记录日志不中断
7. 生命周期方法：start() 订阅 / stop() 取消订阅
8. 领域状态说明：_handlers 记录订阅回调引用
"""
import logging

from src.shared.events import (
    CHARACTER_PRESENCE_CHANGED,
    COST_CIRCUIT_OPEN,
    COST_MILESTONE,
    DEGRADATION_CHANGED,
    SESSION_STATE_CHANGED,
    SESSION_SWITCHED,
    SPEECH_ARBITRATED,
    SPEECH_COMPLETED,
    STATE_CHANGED,
    SWITCH_CHANGED,
    WATCHDOG_CHANGED,
)

logger = logging.getLogger(__name__)

# 触发事件清单（六类 10 个：开关 / 会话 / 在场发言 / 降级 / watchdog / 成本）
_TRIGGER_EVENTS = [
    SWITCH_CHANGED,
    SESSION_SWITCHED,
    SESSION_STATE_CHANGED,
    CHARACTER_PRESENCE_CHANGED,
    SPEECH_ARBITRATED,
    SPEECH_COMPLETED,
    DEGRADATION_CHANGED,
    WATCHDOG_CHANGED,
    COST_CIRCUIT_OPEN,
    COST_MILESTONE,
]


class StatePublisher:
    """状态变更 → state:changed 全量快照发布器。"""

    def __init__(self, event_bus, state_provider):
        self._event_bus = event_bus
        self._provider = state_provider
        self._handlers = {}

    def start(self) -> None:
        for evt in _TRIGGER_EVENTS:
            self._handlers[evt] = lambda event=evt, **kw: self._on_change(event, **kw)
            try:
                self._event_bus.subscribe(evt, self._handlers[evt], name=f"StatePublisher:{evt}")
            except ValueError:
                logger.warning("[StatePublisher] 事件 %s 未注册，跳过", evt)
        logger.info("[StatePublisher] 已启动，订阅 %d 类触发事件", len(_TRIGGER_EVENTS))

    def stop(self) -> None:
        for evt, handler in self._handlers.items():
            self._event_bus.unsubscribe(evt, handler)
        self._handlers.clear()

    def _on_change(self, event: str, **data) -> None:
        try:
            snapshot = self._provider.snapshot()
        except Exception as e:
            logger.error("[StatePublisher] 快照生成失败: %s", e)
            return
        self._event_bus.publish(STATE_CHANGED, snapshot=snapshot, trigger=event)
