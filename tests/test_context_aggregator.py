"""test_context_aggregator.py — 上下文聚合"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.commander.input.context_aggregator import ContextAggregator


class FakeMemory:
    async def handle(self, command):
        if command["capability"] == "memory:get_history":
            return {"ok": True, "data": {"history": [
                {"role": "user", "content": "刚说过的话"},
                {"role": "assistant", "content": "回复内容"}]}}
        return {"ok": True, "data": {"memories": []}}


class FakeSession:
    role = "yuki"
    scene = "chat"
    live_mode = "offline"

    def snapshot(self):
        return {"role": self.role, "scene": self.scene, "live_mode": self.live_mode}


def _agg(memory=None, session=None):
    return ContextAggregator(memory=memory or FakeMemory(), session=session or FakeSession())


def test_build_merges_memory_session_and_reference():
    agg = _agg()
    ctx = asyncio.run(agg.build(role="yuki", reference=[{"title": "脚本1"}]))
    assert "history" in ctx and len(ctx["history"]) == 2
    assert ctx["session"]["role"] == "yuki"
    assert ctx["reference"] == [{"title": "脚本1"}]
    assert "snapshot_ts" in ctx


def test_build_without_memory_safe():
    agg = ContextAggregator(memory=None, session=None)
    ctx = asyncio.run(agg.build())
    assert ctx["history"] == []
    assert ctx["session"] == {}
    assert ctx["reference"] == []


def test_build_publishes_context_snapshot():
    from src.shared.event_bus import EventBus
    from src.shared.events import CONTEXT_SNAPSHOT_READY
    bus = EventBus()
    bus.reset()
    seen = {}
    bus.subscribe(CONTEXT_SNAPSHOT_READY, lambda event, **kw: seen.update(kw))
    agg = ContextAggregator(memory=FakeMemory(), session=FakeSession(), event_bus=bus)
    asyncio.run(agg.build())
    assert "history" in seen["context"]
