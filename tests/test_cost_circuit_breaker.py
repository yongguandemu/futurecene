"""test_cost_circuit_breaker.py — 成本追踪 + 熔断器（P5 验收：熔断后拦截 LLM 调用）"""
from src.commander.cost_circuit_breaker import CostCircuitBreaker
from src.commander.cost_tracker import CostTracker
from src.shared.event_bus import EventBus
from src.shared.events import COST_CIRCUIT_OPEN


def test_cost_tracker_accumulates(tmp_path):
    tracker = CostTracker(persist=False)
    tracker.record("llm", model="gpt-4o-mini", prompt_tokens=1000, completion_tokens=1000)
    tracker.record("llm", model="gpt-4o-mini", prompt_tokens=1000, completion_tokens=1000)
    snap = tracker.snapshot()
    assert snap["total_calls"] == 2
    assert snap["by_type"]["llm"]["tokens"] == 4000
    assert snap["total_cost"] > 0


def test_tts_cost_by_chars():
    tracker = CostTracker(persist=False)
    tracker.record("tts", chars=1000)
    snap = tracker.snapshot()
    assert snap["by_type"]["tts"]["calls"] == 1
    assert snap["total_cost"] > 0


def test_circuit_breaker_opens_on_daily_limit():
    bus = EventBus()
    bus.reset()
    seen = {}
    bus.subscribe(COST_CIRCUIT_OPEN, lambda event, **kw: seen.update(kw))
    breaker = CostCircuitBreaker(bus, daily_limit=0.001)
    assert breaker.record(0.001) is True  # 未超限
    assert breaker.record(0.001) is False  # 超限 → 熔断
    assert breaker.is_open is True
    assert seen["reason"] == "日预算超限"


def test_should_block_after_open():
    bus = EventBus()
    bus.reset()
    breaker = CostCircuitBreaker(bus, daily_limit=0.001)
    breaker.record(0.002)
    # 指挥官在调用 LLM/TTS 前的拦截检查
    assert breaker.should_block() is True


def test_reset_clears_open():
    bus = EventBus()
    bus.reset()
    breaker = CostCircuitBreaker(bus, daily_limit=0.001)
    breaker.record(0.002)
    breaker.reset()
    assert breaker.is_open is False
    assert breaker.should_block() is False


def test_snapshot():
    bus = EventBus()
    bus.reset()
    breaker = CostCircuitBreaker(bus, daily_limit=1.0, monthly_limit=10.0)
    breaker.record(0.5)
    snap = breaker.snapshot()
    assert snap["daily_cost"] == 0.5
    assert snap["open"] is False
