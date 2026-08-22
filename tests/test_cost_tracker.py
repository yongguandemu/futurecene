"""test_cost_tracker.py — 成本追踪单测（含任务五按日统计 get_stats）"""
import time

from src.commander.cost_tracker import CostTracker


def _make(tmp_path):
    return CostTracker(persist=False, persist_path=str(tmp_path / "cost.json"))


def test_record_and_snapshot(tmp_path):
    tracker = _make(tmp_path)
    cost = tracker.record("llm", provider="openai", model="deepseek-v4-pro",
                          prompt_tokens=1000, completion_tokens=1000)
    assert cost > 0
    snap = tracker.snapshot()
    assert snap["total_calls"] == 1
    assert snap["by_type"]["llm"]["calls"] == 1


def test_get_stats_today(tmp_path):
    tracker = _make(tmp_path)
    tracker.record("llm", model="gpt-4o-mini", prompt_tokens=500, completion_tokens=500)
    tracker.record("tts", chars=100)
    stats = tracker.get_stats("today")
    assert stats["date"] == time.strftime("%Y-%m-%d")
    assert stats["total_calls"] == 2
    assert set(stats["by_type"].keys()) == {"llm", "tts"}
    # 与 snapshot 总量一致
    assert abs(stats["total_cost"] - tracker.snapshot()["total_cost"]) < 1e-9


def test_get_stats_yesterday_empty(tmp_path):
    tracker = _make(tmp_path)
    stats = tracker.get_stats("yesterday")
    assert stats["total_calls"] == 0
    assert stats["by_type"] == {}


def test_get_stats_specific_date(tmp_path):
    tracker = _make(tmp_path)
    tracker.record("llm", model="gpt-4o-mini", prompt_tokens=100, completion_tokens=100)
    today = time.strftime("%Y-%m-%d")
    stats = tracker.get_stats(today)
    assert stats["total_calls"] == 1
    assert tracker.get_stats("2099-01-01")["total_calls"] == 0


def test_daily_accumulates_separate_days(tmp_path):
    tracker = _make(tmp_path)
    tracker.record("llm", model="gpt-4o-mini", prompt_tokens=100, completion_tokens=100)
    # 手动把一条记录改到昨天（直接改 _daily 模拟跨天）
    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    with tracker._lock:
        tracker._daily[yesterday] = {
            "by_type": {"llm": {"cost": 0.1, "calls": 1, "tokens": 100}},
            "total_cost": 0.1, "total_calls": 1, "date": yesterday}
    assert tracker.get_stats("today")["total_calls"] == 1
    assert tracker.get_stats("yesterday")["total_calls"] == 1
    assert tracker.snapshot()["total_calls"] == 1  # 全局累计仅 record 统计


def test_reset_clears_daily(tmp_path):
    tracker = _make(tmp_path)
    tracker.record("llm", model="gpt-4o-mini", prompt_tokens=100, completion_tokens=100)
    tracker.reset()
    assert tracker.get_stats("today")["total_calls"] == 0
    assert tracker.snapshot()["total_calls"] == 0
