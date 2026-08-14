"""state_provider.py — 系统状态快照聚合（前端重构 · 方案 A）

统一生成 {version, session, switches, orchestrators, degradation, cost, watchdog, characters}，
供 /api/state、/api/metrics 与 state:changed 事件共用，保证全链路同一份快照。

# 模块内容清单（8 项契约）
1. 模块身份标识：web · StateProvider · 对外 snapshot()
2. 配置契约：构造注入 event_bus/session/switch_manager/registry/degradation_manager/metrics_provider/characters_provider
3. 输入契约：snapshot() 无参数
4. 输出契约：{version, session, switches, orchestrators, degradation, cost, watchdog, characters}；version=event_bus.current_seq()
5. 依赖声明：typing、src.shared.event_bus
6. 错误定义：组件缺失时对应字段返回空（{} / []）；characters_provider 未注入时以 session.present_roles 兜底派生
7. 生命周期方法：snapshot()（无状态）
8. 领域状态说明：无模块级可变状态
"""
from typing import Any, Callable, Dict, Optional


class StateProvider:
    """系统状态快照聚合器（唯一快照来源）。"""

    def __init__(self, event_bus, session=None, switch_manager=None,
                 registry=None, degradation_manager=None,
                 metrics_provider=None,
                 characters_provider: Optional[Callable[[], Dict[str, dict]]] = None):
        self._event_bus = event_bus
        self._session = session
        self._switch_manager = switch_manager
        self._registry = registry
        self._degradation = degradation_manager
        self._metrics = metrics_provider
        self._characters: Optional[Callable[[], Dict[str, dict]]] = characters_provider

    def snapshot(self) -> Dict[str, Any]:
        version = self._event_bus.current_seq() if self._event_bus else 0
        metrics = self._metrics() if self._metrics else {}
        session_snap = self._session.snapshot() if self._session else {}
        characters = self._characters() if self._characters else {}
        # 仅未注入 characters_provider 时以 session.present_roles 兜底派生；
        # provider 明确返回空 dict 时保持权威空值，不参与兜底。
        if self._characters is None and session_snap:
            characters = {r: {"present": True}
                          for r in session_snap.get("present_roles", [])}
        return {
            "version": version,
            "session": session_snap,
            "switches": self._switch_manager.snapshot() if self._switch_manager else {},
            "orchestrators": [o.name for o in self._registry.all()]
            if self._registry else [],
            "degradation": self._degradation.snapshot() if self._degradation else {},
            "cost": metrics.get("cost", {}),
            "watchdog": metrics.get("watchdog", {}),
            "characters": characters,
        }
