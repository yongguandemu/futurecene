"""cost_circuit_breaker.py — 成本熔断器（P5，旧项目 cost_circuit_breaker.py 模式）

日/月预算超限时自动熔断，发布 cost:circuit_open（规格书 962 行）；
熔断后指挥官在调用 LLM/TTS 前拦截（should_block），返回提示性回复。

# 模块内容清单（8 项契约）
1. 模块身份标识：commander · CostCircuitBreaker · 对外 record/should_block/snapshot/reset
2. 配置契约：构造参数 daily_limit（默认 5.0）/monthly_limit（默认 100.0）USD
3. 输入契约：record(cost) 记录一笔费用
4. 输出契约：record 返回是否保持关闭；should_block/is_open 返回是否熔断；snapshot 返回状态字典；发布 COST_CIRCUIT_OPEN 事件
5. 依赖声明：logging、threading、time、datetime、shared.events
6. 错误定义：日/月预算超限触发熔断（_open），仅首次发布事件
7. 生命周期方法：record()/should_block()/reset()/snapshot()
8. 领域状态说明：_daily_cost/_monthly_cost/_current_day/_open_reason/_notified
"""
import logging
import threading
import time
from datetime import date
from typing import Dict, Optional

from src.shared.events import COST_CIRCUIT_OPEN

logger = logging.getLogger(__name__)


class CostCircuitBreaker:
    """成本熔断器。"""

    def __init__(self, event_bus, daily_limit: float = 5.0, monthly_limit: float = 100.0):
        self._event_bus = event_bus
        self._daily_limit = daily_limit  # USD
        self._monthly_limit = monthly_limit
        self._lock = threading.RLock()
        self._daily_cost = 0.0
        self._monthly_cost = 0.0
        self._current_day = date.today().isoformat()
        self._open_reason: Optional[str] = None
        self._notified = False

    # ---------- 对外 ----------

    def record(self, cost: float) -> bool:
        """记录一笔费用；返回 True 表示熔断器保持关闭（未超限）。"""
        with self._lock:
            self._roll_day()
            self._daily_cost += cost
            self._monthly_cost += cost
            if self._daily_cost > self._daily_limit:
                self._open("日预算超限")
                return False
            if self._monthly_cost > self._monthly_limit:
                self._open("月预算超限")
                return False
            return True

    def should_block(self) -> bool:
        """指挥官调用 LLM/TTS 前检查：熔断开启则拦截。"""
        with self._lock:
            return self._open_reason is not None

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._open_reason is not None

    def snapshot(self) -> Dict:
        with self._lock:
            return {"open": self._open_reason is not None,
                    "reason": self._open_reason,
                    "daily_cost": self._daily_cost,
                    "daily_limit": self._daily_limit,
                    "monthly_cost": self._monthly_cost,
                    "monthly_limit": self._monthly_limit,
                    "day": self._current_day}

    def reset(self) -> None:
        with self._lock:
            self._daily_cost = 0.0
            self._monthly_cost = 0.0
            self._open_reason = None
            self._notified = False

    # ---------- 内部 ----------

    def _open(self, reason: str) -> None:
        if self._open_reason is None:
            self._open_reason = reason
            logger.warning("[CostCircuitBreaker] 熔断触发: %s", reason)
        if not self._notified:
            self._notified = True
            self._event_bus.publish(COST_CIRCUIT_OPEN, reason=reason,
                                    daily_cost=self._daily_cost,
                                    daily_limit=self._daily_limit)

    def _roll_day(self) -> None:
        today = date.today().isoformat()
        if today != self._current_day:
            self._current_day = today
            self._daily_cost = 0.0
