"""turn_tracker.py — 话轮追踪：谁在说 / 待发队列（优先级插队 + FIFO）/ 冷却 / 话轮历史。"""
import heapq
import threading
import time
from typing import Any, Dict, List, Optional


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

    def release(self, role: str) -> None:
        with self._lock:
            if self._current == role:
                self._current = None
                self._last_speech[role] = time.time()

    @property
    def current_speaker(self) -> Optional[str]:
        with self._lock:
            return self._current

    def enqueue(self, request: Dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            heapq.heappush(self._queue,
                           (request.get("priority", 5), self._seq, request))

    def dequeue(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._current is not None or not self._queue:
                return None
            _, _, request = heapq.heappop(self._queue)
            self._current = request.get("role")
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
