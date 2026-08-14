"""模块内容清单 — heat_tracker

## 1. 模块身份标识
- 所属调度官：live_intelligence_orchestrator
- 能力名：intel:heat_record / heat_score / heat_level / heat_stats / heat_history
- 引擎名：（单实现）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| weights | 否 | 见 DEFAULT_WEIGHTS | dict[str,float] | 各事件类型权重 |
| decay_per_sec | 否 | 0.02 | float, 0-1 | 每秒时间衰减比例 |
| event_decay | 否 | 0.95 | float, 0-1 | 每次事件额外衰减 |

## 3. 输入契约
- intel:heat_record 输入：{"event_type": str, "weight"?: float, "count"?: int}
  - event_type 必填，str ∈ {danmaku,gift,follow,share,like,enter,comment,super_chat}
  - weight 可选，float>0，缺省查表
  - count 可选，int≥1，缺省 1
- heat_score / heat_level / heat_stats / heat_history 输入：无

## 4. 输出契约
- 成功：{"ok": true, "data": {"score": float, "level": str, ...}, "error": null}
- 失败：{"ok": false, "data": {}, "error": str}

## 5. 依赖声明
- 外部服务：无
- 内部模块：shared/events（HEAT_UPDATED）、shared/event_bus（可选）
- 预先配置：无

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| ValueError | event_type 为空 | 调用方校验输入 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| init | 是 | 初始化分数、权重、衰减参数 |
| start | 否 | 订阅观众事件自动累计（由调度官调用） |
| stop | 否 | 退订事件 |
| health | 是 | 返回当前分数与等级 |

## 8. 领域状态说明
- 状态项：_score、_count、_peak、_score_samples（趋势）、_events
- 持久化：无
- 恢复：无
"""
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from src.shared.events import HEAT_UPDATED

logger = logging.getLogger(__name__)


class HeatTracker:
    """直播间热度追踪器 — 事件加权累加 + 时间衰减 + 热度等级。

    分数模型：score = score * event_decay + total_weight，并随时间指数衰减。
    """

    DEFAULT_WEIGHTS: Dict[str, float] = {
        "danmaku": 1.0, "gift": 8.0, "follow": 5.0, "share": 4.0,
        "like": 2.0, "enter": 0.5, "comment": 1.5, "super_chat": 10.0,
    }
    LEVEL_THRESHOLDS = [
        ("boiling", 100.0), ("hot", 50.0), ("warm", 20.0),
        ("cool", 5.0), ("cold", 0.0),
    ]

    def __init__(self, event_bus=None, decay_per_sec: float = 0.02,
                 event_decay: float = 0.95):
        self.event_bus = event_bus
        self._lock = threading.RLock()
        self._score = 0.0
        self._count = 0
        self._events: List[Dict[str, Any]] = []
        self._events_limit = 200
        self._type_counts: Dict[str, int] = {}
        self._last_update = time.time()
        self._decay_per_sec = float(decay_per_sec)
        self._event_decay = float(event_decay)
        self._peak = 0.0
        self._peak_time = 0.0
        self._score_samples: List[tuple] = []
        self._weights: Dict[str, float] = dict(self.DEFAULT_WEIGHTS)
        self._subscribed = False
        logger.info("[HeatTracker] 初始化完成 (decay_per_sec=%.3f)", self._decay_per_sec)

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """订阅观众事件自动累计（幂等）。"""
        if self._subscribed or self.event_bus is None:
            return
        mapping = {
            "danmaku:received": "danmaku",
            "gift:received": "gift",
            "guard:received": "follow",
            "superchat:received": "super_chat",
            "audience:entered": "enter",
        }
        try:
            for event, etype in mapping.items():
                self.event_bus.subscribe(event, self._make_handler(etype), priority=40)
            self._subscribed = True
            logger.info("[HeatTracker] 已订阅观众事件")
        except Exception as e:
            logger.warning("[HeatTracker] 订阅失败: %s", e)

    def stop(self) -> None:
        if not self._subscribed or self.event_bus is None:
            return
        mapping = ["danmaku:received", "gift:received", "guard:received",
                   "superchat:received", "audience:entered"]
        try:
            for event in mapping:
                self.event_bus.unsubscribe(event, self._make_handler(None))
            self._subscribed = False
        except Exception as e:
            logger.warning("[HeatTracker] 退订失败: %s", e)

    def health(self) -> Dict[str, Any]:
        return {"status": "ok", "detail": f"score={self.get_score():.1f}, {self.get_stats()}"}

    # ---------- 事件回调 ----------

    def _make_handler(self, etype):
        def handler(event: str = "", **kwargs) -> None:
            self.record_event(etype)
        return handler

    # ---------- 核心操作 ----------

    def record_event(self, event_type: str, weight: Optional[float] = None,
                     count: int = 1) -> float:
        """记录一个热度事件，返回新分数。"""
        event_type = (event_type or "").strip()
        if not event_type:
            raise ValueError("event_type must be non-empty")
        now = time.time()
        with self._lock:
            self._apply_time_decay(now)
            if weight is None:
                weight = self._weights.get(event_type, 1.0)
            total = float(weight) * max(1, int(count))
            self._score = self._score * self._event_decay + total
            self._count += max(1, int(count))
            self._type_counts[event_type] = self._type_counts.get(event_type, 0) + max(1, int(count))
            self._last_update = now
            if self._score > self._peak:
                self._peak = self._score
                self._peak_time = now
            self._events.append({"type": event_type, "weight": total, "ts": now})
            if len(self._events) > self._events_limit:
                self._events = self._events[-self._events_limit:]
            self._score_samples.append((now, self._score))
            if len(self._score_samples) > 500:
                self._score_samples = self._score_samples[-500:]
        if self.event_bus:
            try:
                self.event_bus.publish(HEAT_UPDATED, score=self._score,
                                       level=self.get_level(), event_type=event_type)
            except Exception as e:
                logger.warning("[HeatTracker] 发布事件失败: %s", e)
        return self._score

    def get_score(self) -> float:
        with self._lock:
            self._apply_time_decay(time.time())
            return round(self._score, 2)

    def get_level(self) -> str:
        score = self.get_score()
        for name, threshold in self.LEVEL_THRESHOLDS:
            if score >= threshold:
                return name
        return "cold"

    def get_trend(self, window: float = 60.0) -> float:
        """返回最近 window 秒的分数变化率（正=上升，负=下降）。"""
        with self._lock:
            now = time.time()
            base = now - window
            recent = [s for (t, s) in self._score_samples if t >= base]
            if len(recent) < 2:
                return 0.0
            return round(recent[-1] - recent[0], 2)

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            self._apply_time_decay(time.time())
            return {
                "score": round(self._score, 2),
                "level": self._level_locked(),
                "count": self._count,
                "peak": round(self._peak, 2),
                "type_counts": dict(self._type_counts),
                "trend_60s": round(self._trend_locked(60.0), 2),
            }

    def get_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._events[-limit:]]

    # ---------- 内部 ----------

    def _apply_time_decay(self, now: float) -> None:
        dt = max(0.0, now - self._last_update)
        if dt > 0:
            self._score *= (1.0 - self._decay_per_sec) ** dt
            self._last_update = now

    def _level_locked(self) -> str:
        for name, threshold in self.LEVEL_THRESHOLDS:
            if self._score >= threshold:
                return name
        return "cold"

    def _trend_locked(self, window: float) -> float:
        base = time.time() - window
        recent = [s for (t, s) in self._score_samples if t >= base]
        if len(recent) < 2:
            return 0.0
        return recent[-1] - recent[0]