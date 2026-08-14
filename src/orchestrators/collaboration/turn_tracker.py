"""turn_tracker.py — 话轮追踪：谁在说 / 待发队列（优先级插队 + FIFO）/ 冷却 / 话轮历史。

dequeue() 返回 None 的两种情形：
1. 待发队列为空（pending_count() == 0）；
2. 互斥锁仍被占用（_current 非 None），需先由持有者 release 释放。

队列仲裁由协调器在 release 后驱动：release() 返回 True（成功释放）后，
协调器应调用 dequeue() 取出下一话轮。

# 模块内容清单 — turn_tracker

## 1. 模块身份标识
- 所属调度官：collaboration（多角色协作域）
- 能力名：collab:arbitrate 的互斥/队列支撑（间接）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| 无 | - | - | - | 纯内存数据结构，构造即就绪 |

## 3. 输入契约
- 输入格式：`acquire(role)` / `release(role)` / `enqueue(request)` / `dequeue()` / `idle_seconds(role)` / `record_turn(role, kind, ref_text, text)` / `turn_history(limit)` / `current_speaker` / `pending_count()`
- role：str，角色名；request：dict（含 role/priority/text/ref_text）

## 4. 输出契约
- 成功：`acquire()/release()/enqueue()` 返回 bool；`dequeue()` 返回队首 request 或 `None`；`idle_seconds()` 返回 float（未发言为 inf）；`turn_history()` 返回 dict 列表；`current_speaker` 返回 str 或 `None`
- 失败：`release()` 角色不匹配返回 `False` 并警告；`enqueue()` 缺 role 拒绝返回 `False`
- 事件：无

## 5. 依赖声明
- 外部服务：无
- 内部模块：heapq、threading、time、logging（纯标准库）
- 预先配置：无

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 释放角色不匹配 | release 时 _current != role | 返回 False，记录警告 |
| 入队拒绝 | request 缺 role 或非字符串 | 返回 False，记录警告 |
| 队列堆积 | 长期无人 release | 由 coordinator 保证 finally 释放互斥 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start/stop | 否 | 无（纯数据结构，随仲裁器生命周期） |

## 8. 领域状态说明
- 状态项：`_current`（当前发言人）、`_queue`（优先级堆）、`_last_speech`（角色→最近发言时间）、`_history`（话轮历史，上限 200）
- 持久化：无
- 恢复：无（互斥与队列随进程生命周期）
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
