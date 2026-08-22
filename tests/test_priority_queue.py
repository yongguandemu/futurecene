"""test_priority_queue.py — 输入优先级队列"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.commander.input.input_classifier import (
    InputClassifier, InputEnvelope, InputType, PRIORITY,
)
from src.commander.input.priority_queue import PriorityQueue


def _env(t, **kw):
    """直接构造信封（绕过 classify 边界判定，队列测试聚焦排序/插队）。"""
    return InputEnvelope(input_type=t, priority=PRIORITY[t], source=t.value,
                         loop_depth=kw.get("loop_depth", 0),
                         payload={"text": kw.get("text", "")})


def test_push_pop_by_priority():
    q = PriorityQueue()
    q.push(_env(InputType.AUDIENCE))
    q.push(_env(InputType.EXTERNAL_APP))
    q.push(_env(InputType.SYSTEM_LOOP, loop_depth=1))
    assert q.pop().input_type == InputType.EXTERNAL_APP   # P2 先出
    assert q.pop().input_type == InputType.AUDIENCE       # P1
    assert q.pop().input_type == InputType.SYSTEM_LOOP    # P3


def test_same_priority_fifo():
    q = PriorityQueue()
    q.push(_env(InputType.AUDIENCE, text="a"))
    q.push(_env(InputType.AUDIENCE, text="b"))
    assert q.pop().payload["text"] == "a"
    assert q.pop().payload["text"] == "b"


def test_operator_insert_front():
    q = PriorityQueue()
    q.push(_env(InputType.AUDIENCE, text="弹幕"))
    q.insert_front(_env(InputType.OPERATOR, text="!点歌"))
    assert q.pop().payload["text"] == "!点歌"


def test_loop_depth_exceeded_rejected():
    q = PriorityQueue(max_loop_depth=5)
    assert q.push(_env(InputType.SYSTEM_LOOP, loop_depth=5)) is True   # 等于上限可入
    assert q.push(_env(InputType.SYSTEM_LOOP, loop_depth=6)) is False  # 超限拒绝


def test_operator_bypasses_depth_limit():
    q = PriorityQueue(max_loop_depth=5)
    assert q.insert_front(_env(InputType.OPERATOR, loop_depth=99)) is True
    assert q.size() == 1


def test_empty_pop_returns_none():
    q = PriorityQueue()
    assert q.pop() is None
    assert q.size() == 0


def test_snapshot_counts_by_type():
    q = PriorityQueue()
    q.push(_env(InputType.AUDIENCE))
    q.push(_env(InputType.AUDIENCE))
    q.push(_env(InputType.EXTERNAL_APP))
    assert q.snapshot()["audience"] == 2
    assert q.snapshot()["external_app"] == 1
