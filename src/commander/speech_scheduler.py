"""speech_scheduler.py — 发言时间线调度（任务二，指挥官层）

主动/被动发言分离：
- 主动发言（BatchPlanner 预生成批）入队，按空档逐个出队播放，超时过期丢弃
- 被动发言（弹幕回复等）实时插入：空闲直接播（speech:inserted），否则排队空档，不抢占
- 覆盖丢弃：主动项被被动发言推迟 ≥3 次 → 丢弃
- TTS 合成时机（QA Q3）：scheduler 决定「即将播放」（窗口 1 条）才发布 speech:scheduled，
  由消费方（danmaku_pipeline）在播放前合成，避免过期丢弃浪费合成成本

# 模块内容清单 — speech_scheduler

## 1. 模块身份标识
- 所属调度官：commander · speech_scheduler · 能力 无（指挥官层内部调度）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| active_ttl | 否 | 150.0 | float | 主动项过期秒数（2-3 分钟窗口） |
| passive_ttl | 否 | 90.0 | float | 被动项过期秒数 |
| max_cover | 否 | 3 | int | 被被动发言推迟次数上限（超则丢弃） |

## 3. 输入契约
- submit_batch(plans: list[{text,mood,suggested_window_sec,duration_estimate}], role) -> int
- submit_passive(text, role) -> dict（uid/queued）
- tick(now: float) -> list[事件]（内部发布事件）
- complete(uid) -> None

## 4. 输出契约
- 成功：submit_batch 返回入队条数；tick 返回本轮出队/丢弃的事件清单
- 失败：无（队列操作纯内存，永不抛错）

## 5. 依赖声明
- 外部服务：无
- 内部模块：src.shared.events（事件常量）；可选 switch_check（fn(name)->bool）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 事件发布异常 | EventBus 未就绪 | 捕获并记录，调度不中断 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| tick | 是 | 清过期 + 出队下一条（每空档调用一次） |
| complete | 是 | 标记当前发言完成，释放空档 |

## 8. 领域状态说明
- 状态项：_queue（deque）、_playing（当前发言或 None）
- 持久化：无（内存队列）
- 恢复：重启后队列清空（预生成批随进程丢失，可重新生成）
"""
import logging
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

from src.shared.events import (
    SPEECH_DEQUEUED,
    SPEECH_ENQUEUED,
    SPEECH_INSERTED,
    SPEECH_SCHEDULED,
)

logger = logging.getLogger(__name__)


class SpeechScheduler:
    """发言时间线：主动批队列 + 被动实时插入 + 过期/覆盖丢弃。"""

    def __init__(self, event_bus=None, active_ttl: float = 150.0,
                 passive_ttl: float = 90.0, max_cover: int = 3,
                 switch_check: Optional[Callable[[str], bool]] = None) -> None:
        self._bus = event_bus
        self._active_ttl = float(active_ttl)
        self._passive_ttl = float(passive_ttl)
        self._max_cover = max(1, int(max_cover))
        self._switch_check = switch_check  # fn(name) -> bool；None 视为全开
        self._queue: Deque[Dict[str, Any]] = deque()
        self._playing: Optional[Dict[str, Any]] = None
        # RLock：事件同步回调（complete）会重入本锁（tick 持锁发布 → handler 回执）
        self._lock = threading.RLock()
        self._seq = 0

    # ---------- 对外接口 ----------

    def submit_batch(self, plans: List[Dict[str, Any]], role: str = "") -> int:
        """主动批入队（batch_mode 关时忽略）。返回实际入队条数。"""
        if not self._switch_on("batch_mode"):
            return 0
        count = 0
        with self._lock:
            for plan in plans or []:
                text = str((plan or {}).get("text", "")).strip()
                if not text:
                    continue
                self._queue.append({
                    "uid": self._new_uid("a"),
                    "kind": "active",
                    "text": text,
                    "mood": str((plan or {}).get("mood", "default")),
                    "role": role,
                    "enqueued_at": time.time(),
                    "miss_count": 0,
                    "ttl": self._active_ttl,
                    "duration_estimate": float((plan or {}).get("duration_estimate", 8.0)),
                })
                count += 1
        if count:
            logger.info("[SpeechScheduler] 主动批入队 %d 条 role=%s", count, role or "default")
        return count

    def submit_passive(self, text: str, role: str = "") -> Dict[str, Any]:
        """被动发言插入：空闲直接播（不抢占正在播放），否则排队并推迟主动项。"""
        text = (text or "").strip()
        if not text:
            return {"uid": "", "queued": False}
        item = {"uid": self._new_uid("p"), "kind": "passive", "text": text,
                "mood": "default", "role": role, "enqueued_at": time.time(),
                "miss_count": 0, "ttl": self._passive_ttl}
        with self._lock:
            if self._playing is None:
                # 空档：直接排期播放（real_time_mode 关时改为排队）
                if not self._switch_on("real_time_mode"):
                    self._queue.append(item)
                    self._publish(SPEECH_ENQUEUED, uid=item["uid"], kind="passive",
                                  role=role or "default")
                    return {"uid": item["uid"], "queued": True}
                self._playing = item
                self._publish(SPEECH_INSERTED, uid=item["uid"], text=text,
                              role=role or "default")
                self._publish(SPEECH_SCHEDULED, uid=item["uid"], text=text,
                              mood="default", role=role or "default",
                              kind="passive", duration_estimate=8.0)
                return {"uid": item["uid"], "queued": False}
            # 正在播放：排队；同时把队列中所有主动项视为被推迟一次（覆盖计数）
            for it in self._queue:
                if it["kind"] == "active":
                    it["miss_count"] += 1
            self._queue.append(item)
            self._publish(SPEECH_ENQUEUED, uid=item["uid"], kind="passive",
                          role=role or "default")
        return {"uid": item["uid"], "queued": True}

    def tick(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """调度一步：清过期 → 空档出队。返回本轮事件清单（测试断言用）。"""
        now = now if now is not None else time.time()
        events: List[Dict[str, Any]] = []
        with self._lock:
            self._drop_expired(now, events)
            if self._playing is not None:
                return events
            while self._queue:
                item = self._queue.popleft()
                # 覆盖丢弃：主动项被推迟过多次（被大量被动发言挤掉）
                if item["kind"] == "active" and item["miss_count"] >= self._max_cover:
                    self._publish(SPEECH_DEQUEUED, uid=item["uid"], kind="active",
                                  discarded="covered")
                    events.append({"event": "dequeued", "reason": "covered",
                                   "uid": item["uid"]})
                    continue
                self._playing = item
                self._publish(SPEECH_ENQUEUED, uid=item["uid"], kind=item["kind"],
                              role=item["role"] or "default")
                self._publish(SPEECH_SCHEDULED, uid=item["uid"], text=item["text"],
                              mood=item["mood"], role=item["role"] or "default",
                              kind=item["kind"],
                              duration_estimate=item.get("duration_estimate", 8.0))
                events.append({"event": "scheduled", "uid": item["uid"],
                               "kind": item["kind"]})
                break
        return events

    def complete(self, uid: str) -> None:
        """当前发言完成，释放空档（uid 不匹配时忽略，防止乱序）。"""
        with self._lock:
            if self._playing and self._playing["uid"] == uid:
                self._playing = None

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "playing": dict(self._playing) if self._playing else None,
                "queue_size": len(self._queue),
                "queue": [{"uid": i["uid"], "kind": i["kind"],
                           "text": i["text"][:30], "miss": i["miss_count"]}
                          for i in list(self._queue)],
            }

    # ---------- 内部 ----------

    def _drop_expired(self, now: float, events: List[Dict[str, Any]]) -> None:
        keep: Deque[Dict[str, Any]] = deque()
        while self._queue:
            item = self._queue.popleft()
            if now - item["enqueued_at"] > item["ttl"]:
                self._publish(SPEECH_DEQUEUED, uid=item["uid"], kind=item["kind"],
                              discarded="expired")
                events.append({"event": "dequeued", "reason": "expired",
                               "uid": item["uid"]})
            else:
                keep.append(item)
        self._queue = keep

    def _switch_on(self, name: str) -> bool:
        try:
            return self._switch_check(name) if self._switch_check else True
        except Exception:
            return True

    def _new_uid(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{int(time.time() * 1000)}-{self._seq}"

    def _publish(self, event: str, **data) -> None:
        if self._bus is None:
            return
        try:
            self._bus.publish(event, **data)
        except Exception as e:
            logger.warning("[SpeechScheduler] 发布 %s 失败: %s", event, e)
