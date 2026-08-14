"""turn_tracker 单测。"""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.collaboration.turn_tracker import TurnTracker


def test_acquire_release_mutex():
    tt = TurnTracker()
    assert tt.acquire("yuki") is True
    assert tt.acquire("lilith") is False   # 互斥
    assert tt.release("lilith") is False   # 角色不匹配 → 不释放
    assert tt.release("yuki") is True
    assert tt.acquire("lilith") is True


def test_idle_seconds():
    tt = TurnTracker()
    tt.acquire("yuki")
    time.sleep(0.02)
    tt.release("yuki")
    time.sleep(0.02)                                   # 留出可测的闲置窗口（防时钟粒度归零）
    assert tt.idle_seconds("lilith") == float("inf")   # 从未发言 → 无穷大
    assert 0 < tt.idle_seconds("yuki") < 0.1           # 刚释放 → 冷却刚起步


def test_pending_queue_priority():
    tt = TurnTracker()
    tt.acquire("yuki")
    tt.enqueue({"role": "lilith", "priority": 3, "request_id": "a"})
    tt.enqueue({"role": "yuki", "priority": 0, "request_id": "b"})   # 高优插队
    tt.enqueue({"role": "lilith", "priority": 3, "request_id": "c"})
    tt.release("yuki")
    nxt = tt.dequeue()
    assert nxt["request_id"] == "b"   # P0 优先于先到的 P3
    tt.release("yuki")
    nxt = tt.dequeue()
    assert nxt["request_id"] == "a"   # 同优先级 FIFO：a 先于 c
    tt.release("lilith")
    nxt = tt.dequeue()
    assert nxt["request_id"] == "c"


def test_history_records_turns():
    tt = TurnTracker()
    tt.record_turn("yuki", "danmaku", ref_text="")
    tt.record_turn("lilith", "banter", ref_text="哈哈")
    hist = tt.turn_history()
    assert hist[-1]["role"] == "lilith" and hist[-1]["kind"] == "banter"
