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
    tt.release("yuki")
    assert tt.acquire("lilith") is True


def test_idle_seconds():
    tt = TurnTracker()
    tt.acquire("yuki")
    time.sleep(0.02)
    tt.release("yuki")
    assert tt.idle_seconds("lilith") >= tt.idle_seconds("yuki")


def test_pending_queue_priority():
    tt = TurnTracker()
    tt.acquire("yuki")
    tt.enqueue({"role": "lilith", "priority": 3, "request_id": "a"})
    tt.enqueue({"role": "yuki", "priority": 0, "request_id": "b"})   # 高优插队
    tt.enqueue({"role": "lilith", "priority": 3, "request_id": "c"})
    tt.release("yuki")
    nxt = tt.dequeue()
    assert nxt["request_id"] == "b"   # P0 优先于先到的 P3


def test_history_records_turns():
    tt = TurnTracker()
    tt.record_turn("yuki", "danmaku", ref_text="")
    tt.record_turn("lilith", "banter", ref_text="哈哈")
    hist = tt.turn_history()
    assert hist[-1]["role"] == "lilith" and hist[-1]["kind"] == "banter"
