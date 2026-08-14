"""turn_tracker.py — 话轮追踪：谁在说 / 待发队列（优先级插队 + FIFO）/ 冷却 / 话轮历史。

dequeue() 返回 None 的两种情形：
1. 待发队列为空（pending_count() == 0）；
2. 互斥锁仍被占用（_current 非 None），需先由持有者 release 释放。

队列仲裁由协调器在 release 后驱动：release() 返回 True（成功释放）后，
协调器应调用 dequeue() 取出下一话轮。
"""
import heapq
import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class TurnTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._current: Optional[str] = None
        self._queue: List[tuple] = []          # (priority, seq, request)
        self._seq = 0
        self._last_speech: Dict[str, float] = {}
        self._history: List[Dict[str, Any]] = []

    def acquire(self, role: str) -> bool:
        with self._lock:
            if self._current is not None:
                return False
            self._current = role
            return True

    def release(self, role: str) -> bool:
        with self._lock:
            if self._current == role:
                self._current = None
                self._last_speech[role] = time.time()
                return True
            logger.warning("release 角色不匹配: current=%r, release=%r",
                           self._current, role)
            return False

    @property
    def current_speaker(self) -> Optional[str]:
        with self._lock:
            return self._current

    def enqueue(self, request: Dict[str, Any]) -> bool:
        role = request.get("role")
        if not isinstance(role, str):
            logger.warning("enqueue 拒绝: request 缺少 role 或 role 非字符串: %r",
                           request)
            return False
        with self._lock:
            self._seq += 1
            heapq.heappush(self._queue,
                           (request.get("priority", 5), self._seq, request))
            return True

    def dequeue(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._current is not None or not self._queue:
                return None
            _, _, request = heapq.heappop(self._queue)
            self._current = request["role"]   # enqueue 已校验 role 必为字符串
            return request

    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)

    def idle_seconds(self, role: str) -> float:
        with self._lock:
            last = self._last_speech.get(role)
        return (time.time() - last) if last else float("inf")

    def record_turn(self, role: str, kind: str, ref_text: str = "",
                    text: str = "") -> None:
        with self._lock:
            self._last_speech[role] = time.time()
            self._history.append({"role": role, "kind": kind,
                                  "ref_text": ref_text, "text": text,
                                  "ts": time.time()})
            if len(self._history) > 200:
                self._history = self._history[-200:]

    def turn_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._history[-limit:])
