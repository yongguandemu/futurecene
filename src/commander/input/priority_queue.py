"""priority_queue.py — 输入优先级队列（总控调度化，规格 2026-08-22 任务一）

按优先级排序（P0>P1>P2>P3，同优先级 FIFO）；operator 可直插队首；
系统循环携带深度标记，超过上限拒绝入队（归档短期记忆由调用方决定）。

# 模块内容清单 — priority_queue

## 1. 模块身份标识
- 所属调度官：commander（input 域）
- 能力名：无（指挥官内部服务）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| max_loop_depth | 否 | 5 | int>=1 | 系统自循环深度上限 |

## 3. 输入契约
- push(envelope) -> bool；insert_front(envelope) -> bool；pop() -> Optional[InputEnvelope]

## 4. 输出契约
- 成功：push/insert_front 返回 True；pop 返回队首（无则 None）
- 失败：深度超限返回 False（拒绝入队）

## 5. 依赖声明
- 外部服务：无
- 内部模块：threading、typing、input_classifier.InputEnvelope/InputType

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 深度超限 | system_loop.loop_depth > max_loop_depth | push 返回 False，调用方归档 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | - | 线程安全（RLock），随调随用 |

## 8. 领域状态说明
- 状态项：_buckets（按优先级分桶的 deque）
- 持久化：无
"""
import threading
from collections import deque
from typing import Dict, Optional

from src.commander.input.input_classifier import InputEnvelope, InputType

_ORDERED_TYPES = [InputType.OPERATOR, InputType.EXTERNAL_APP,
                  InputType.AUDIENCE, InputType.SYSTEM_LOOP]  # P0→P3


class PriorityQueue:
    """按优先级排序的输入队列（operator 插队、循环深度上限）。"""

    def __init__(self, max_loop_depth: int = 5):
        self._max_loop_depth = max_loop_depth
        self._buckets: Dict[str, deque] = {t.value: deque() for t in _ORDERED_TYPES}
        self._lock = threading.RLock()

    def push(self, envelope: InputEnvelope) -> bool:
        """入队；system_loop 超深度上限返回 False（拒绝）。"""
        if envelope.input_type == InputType.SYSTEM_LOOP and envelope.loop_depth > self._max_loop_depth:
            return False
        with self._lock:
            self._buckets[envelope.input_type].append(envelope)
        return True

    def insert_front(self, envelope: InputEnvelope) -> bool:
        """operator 直插队首（绕过队列排序，跳过深度限制）。"""
        if envelope.input_type == InputType.OPERATOR:
            with self._lock:
                self._buckets[InputType.OPERATOR.value].appendleft(envelope)
            return True
        return False

    def pop(self) -> Optional[InputEnvelope]:
        """取最高优先级队首（同优先级 FIFO）。空队列返回 None。"""
        with self._lock:
            for t in _ORDERED_TYPES:
                bucket = self._buckets[t.value]
                if bucket:
                    return bucket.popleft()
        return None

    def size(self) -> int:
        with self._lock:
            return sum(len(b) for b in self._buckets.values())

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return {t.value: len(self._buckets[t.value]) for t in _ORDERED_TYPES}
