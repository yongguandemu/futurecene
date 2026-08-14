"""switch_manager.py — 开关集中管理（规格书 4.7 + 1152 行日程驱动）

- 开关项由分 brain 注册自动生成（见 OrchestratorRegistry.register）
- 手动覆盖优先级最高：手动禁用后，任何启用请求均被拒绝
- 支持日程驱动 + 手动覆盖（延续旧项目 feature_switch 验证过的设计）
- 优先级：手动覆盖 > 日程驱动 > 默认状态（规格书 1152 行）

# 模块内容清单（8 项契约）
1. 模块身份标识：commander · SwitchManager · 对外 auto_register/is_enabled/set_manual/set_schedule/check_schedules/snapshot/names
2. 配置契约：无独立配置；开关默认状态由注册时指定（默认 True）
3. 输入契约：set_manual(name, enabled)/set_schedule(name, time_ranges)
4. 输出契约：is_enabled 返回布尔；check_schedules 返回状态字典；发布 SWITCH_CHANGED 事件
5. 依赖声明：logging、datetime、typing、shared.events
6. 错误定义：非法日程时段（HH:MM-HH:MM）抛 ValueError
7. 生命周期方法：auto_register()/auto_unregister()/check_schedules()（定时调用）
8. 领域状态说明：_defaults/_manual/_schedules/_schedule_state（优先级：手动 > 日程 > 默认）
"""
import logging
from datetime import datetime
from typing import Dict, List, Tuple

from src.shared.events import SWITCH_CHANGED

logger = logging.getLogger(__name__)

TimeRange = Tuple[int, int]  # (start_minute, end_minute)；start>end 表示跨天时段


class SwitchManager:
    """统一开关管理。"""

    def __init__(self, event_bus):
        self._event_bus = event_bus
        self._defaults: Dict[str, bool] = {}  # 默认状态（注册时设置）
        self._manual: Dict[str, bool] = {}  # 手动覆盖（优先级最高）
        self._schedules: Dict[str, List[TimeRange]] = {}  # 日程时段（分钟）
        self._schedule_state: Dict[str, bool] = {}  # 最近一次日程计算结果

    def auto_register(self, name: str, default: bool = True) -> None:
        self._defaults[name] = default

    def auto_unregister(self, name: str) -> None:
        """注销分 brain 时移除对应开关项（规格书 4.2 unregister 调用）。

        # TODO: 确认 — 规格书 4.7 未列出本方法，为支撑 4.2 unregister 补齐。
        """
        self._defaults.pop(name, None)
        self._manual.pop(name, None)
        self._schedules.pop(name, None)
        self._schedule_state.pop(name, None)

    def is_enabled(self, name: str) -> bool:
        if name in self._manual:
            return self._manual[name]
        if name in self._schedule_state:
            return self._schedule_state[name]
        return self._defaults.get(name, True)

    def set_manual(self, name: str, enabled: bool) -> None:
        self._manual[name] = enabled
        self._event_bus.publish(SWITCH_CHANGED, name=name, enabled=enabled, source="manual")

    def clear_manual(self, name: str) -> None:
        self._manual.pop(name, None)
        self._event_bus.publish(SWITCH_CHANGED, name=name, enabled=self.is_enabled(name))

    # ---------- 日程驱动（P2，规格书 1152 行） ----------

    @staticmethod
    def _parse_range(text: str) -> TimeRange:
        """解析 "HH:MM-HH:MM" → (start_min, end_min)。"""
        try:
            start_str, end_str = text.split("-")
            sh, sm = start_str.strip().split(":")
            eh, em = end_str.strip().split(":")
            return int(sh) * 60 + int(sm), int(eh) * 60 + int(em)
        except (ValueError, AttributeError) as e:
            raise ValueError(f"非法日程时段: {text!r}（格式 HH:MM-HH:MM）") from e

    def set_schedule(self, name: str, time_ranges: List[str]) -> None:
        """按时间段自动启停。time_ranges 示例：["09:00-17:00", "22:00-02:00"]（支持跨天）。"""
        self._schedules[name] = [self._parse_range(r) for r in time_ranges]
        logger.info("[SwitchManager] 日程已设置: %s %s", name, time_ranges)

    def clear_schedule(self, name: str) -> None:
        self._schedules.pop(name, None)
        self._schedule_state.pop(name, None)

    def check_schedules(self, now: datetime = None) -> Dict[str, bool]:
        """定时调用（如每 30 秒）：按当前时间计算日程状态，变化时发布 SWITCH_CHANGED。

        不触碰手动覆盖（优先级最高）；无日程的开关不受影响。
        """
        now = now or datetime.now()
        minute = now.hour * 60 + now.minute
        result: Dict[str, bool] = {}
        for name, ranges in self._schedules.items():
            enabled = any(self._in_range(minute, r) for r in ranges)
            result[name] = enabled
            if self._schedule_state.get(name) != enabled:
                self._schedule_state[name] = enabled
                logger.info("[SwitchManager] 日程驱动: %s → %s",
                            name, "启用" if enabled else "禁用")
                self._event_bus.publish(SWITCH_CHANGED, name=name, enabled=enabled,
                                        source="schedule")
        return result

    @staticmethod
    def _in_range(minute: int, r: TimeRange) -> bool:
        start, end = r
        if start <= end:
            return start <= minute < end
        return minute >= start or minute < end  # 跨天时段（如 22:00-02:00）

    def snapshot(self) -> Dict[str, bool]:
        return {k: self.is_enabled(k) for k in self._defaults}

    def names(self) -> List[str]:
        """已注册开关名（供降级管理等查询已注册调度官）。"""
        return list(self._defaults.keys())
