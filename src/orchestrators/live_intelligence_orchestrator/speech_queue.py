"""模块内容清单 — speech_queue

## 1. 模块身份标识
- 所属调度官：live_intelligence_orchestrator
- 能力名：intel:speech_enqueue / speech_dequeue / speech_peek / speech_stats / speech_clear
- 引擎名：（单实现）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| max_size | 否 | 200 | int, 1-10000 | 队列最大容量，超出淘汰最旧 |
| timeout | 否 | 120 | float 秒 | 消息超时，超时自动丢弃 |

## 3. 输入契约
- intel:speech_enqueue 输入：{"char_id": str, "text": str, "priority"?: int}
  - char_id 必填，str，非空
  - text 必填，str，非空
  - priority 可选，int，默认 0（越大越优先）
- speech_dequeue 输入：{"limit"?: int}，limit 可选，默认 1，1-100
- speech_peek / speech_stats / speech_clear 输入：无

## 4. 输出契约
- 成功：{"ok": true, "data": {...}, "error": null}
- 失败：{"ok": false, "data": {}, "error": str}

## 5. 依赖声明
- 外部服务：无
- 内部模块：shared/events（SPEECH_ENQUEUED / SPEECH_DEQUEUED）、shared/event_bus（可选）
- 预先配置：无

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| ValueError | text 或 char_id 为空 | 调用方校验输入 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| init | 是 | 初始化 deque 与统计 |
| start/stop | 否 | 纯数据结构，无需生命周期 |
| health | 是 | 返回队列占用与统计 |

## 8. 领域状态说明
- 状态项：_queue、_total_enqueued/_total_dequeued/_total_expired
- 持久化：无
- 恢复：无
"""
import logging
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

from src.shared.events import SPEECH_DEQUEUED, SPEECH_ENQUEUED

logger = logging.getLogger(__name__)


class SpeechQueue:
    """发言队列 — 管理待发言消息的排队、优先级与超时淘汰。thread-safe。"""

    def __init__(self, event_bus=None, max_size: int = 200, timeout: float = 120.0):
        self.event_bus = event_bus
        self._max_size = int(max_size)
        self._timeout = float(timeout)
        self._queue: deque = deque()
        self._lock = threading.RLock()
        self._total_enqueued = 0
        self._total_dequeued = 0
        self._total_expired = 0
        logger.info("[SpeechQueue] 初始化完成 (max_size=%d, timeout=%.0fs)",
                    self._max_size, self._timeout)

    def health(self) -> Dict[str, Any]:
        return {"status": "ok", "detail": f"queue_size={self.size()}, {self.get_stats()}"}

    # ---------- 核心操作 ----------

    def enqueue(self, char_id: str, text: str, priority: int = 0) -> bool:
        """消息入队。返回是否入队成功。"""
        text = (text or "").strip()
        char_id = (char_id or "").strip()
        if not text or not char_id:
            raise ValueError("char_id and text must be non-empty")
        item = {
            "char_id": char_id,
            "text": text,
            "priority": int(priority),
            "timestamp": time.time(),
        }
        with self._lock:
            if len(self._queue) >= self._max_size:
                self._queue.popleft()
                self._total_expired += 1
            if item["priority"] > 0 and self._queue:
                self._insert_by_priority(item)
            else:
                self._queue.append(item)
            self._total_enqueued += 1
        if self.event_bus:
            try:
                self.event_bus.publish(SPEECH_ENQUEUED, char_id=char_id, text=text,
                                       priority=item["priority"])
            except Exception as e:
                logger.warning("[SpeechQueue] 发布事件失败: %s", e)
        return True

    def dequeue(self, limit: int = 1) -> List[Dict[str, Any]]:
        """取出待发言消息（先淘汰超时项，再按优先级顺序取）。"""
        limit = max(1, min(int(limit), self._max_size))
        with self._lock:
            self._expire_locked()
            items = []
            count = 0
            while self._queue and count < limit:
                items.append(self._queue.popleft())
                count += 1
            self._total_dequeued += len(items)
        if items and self.event_bus:
            try:
                self.event_bus.publish(SPEECH_DEQUEUED, count=len(items))
            except Exception as e:
                logger.warning("[SpeechQueue] 发布事件失败: %s", e)
        return items

    def peek(self, limit: int = 5) -> List[Dict[str, Any]]:
        """查看队首（不取出），按优先级顺序。"""
        limit = max(1, min(int(limit), self._max_size))
        with self._lock:
            self._expire_locked()
            return [dict(i) for i in list(self._queue)[:limit]]

    def size(self) -> int:
        with self._lock:
            self._expire_locked()
            return len(self._queue)

    def clear(self) -> int:
        with self._lock:
            n = len(self._queue)
            self._queue.clear()
            self._total_expired += n
        return n

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            self._expire_locked()
            return {
                "size": len(self._queue),
                "max_size": self._max_size,
                "timeout": self._timeout,
                "enqueued": self._total_enqueued,
                "dequeued": self._total_dequeued,
                "expired": self._total_expired,
            }

    # ---------- 内部 ----------

    def _insert_by_priority(self, item: Dict[str, Any]) -> None:
        inserted = False
        temp = deque()
        for front in self._queue:
            if not inserted and front["priority"] < item["priority"]:
                temp.append(item)
                inserted = True
            temp.append(front)
        if not inserted:
            temp.append(item)
        self._queue = temp

    def _expire_locked(self) -> None:
        now = time.time()
        kept = deque()
        for i in self._queue:
            if now - i["timestamp"] <= self._timeout:
                kept.append(i)
            else:
                self._total_expired += 1
        self._queue = kept