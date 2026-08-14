"""test_memory_orchestrator.py — 记忆调度官单测（tmp_path SQLite，真实存取）"""
import asyncio

from src.orchestrators.memory_orchestrator import registry
from src.orchestrators.memory_orchestrator.memory_orchestrator import MemoryOrchestrator
from src.shared.event_bus import EventBus
from src.shared.events import MEMORY_CONSOLIDATED, MEMORY_STORED


def _make(tmp_path):
    bus = EventBus()
    bus.reset()
    return MemoryOrchestrator(event_bus=bus, db_path=str(tmp_path / "m.db")), bus


def test_capabilities_from_registry(tmp_path):
    orch, _ = _make(tmp_path)
    assert orch.capabilities() == registry.capabilities() == [
        "memory:store", "memory:retrieve", "memory:consolidate", "memory:get_history",
    ]


def test_store_and_retrieve(tmp_path):
    orch, bus = _make(tmp_path)
    seen = {}
    bus.subscribe(MEMORY_STORED, lambda event, **kw: seen.update(kw))
    r1 = asyncio.run(orch.handle({"capability": "memory:store",
                                  "payload": {"content": "观众喜欢看VN", "session_id": "s1"}}))
    assert r1["ok"] is True and r1["data"]["memory_id"]
    assert seen["session_id"] == "s1"
    r2 = asyncio.run(orch.handle({"capability": "memory:retrieve",
                                  "payload": {"query": "VN", "session_id": "s1", "k": 3}}))
    assert r2["data"]["memories"]
    assert r2["data"]["memories"][0]["content"] == "观众喜欢看VN"


def test_get_history(tmp_path):
    orch, _ = _make(tmp_path)
    asyncio.run(orch.handle({"capability": "memory:store",
                             "payload": {"content": "第一条", "session_id": "s1"}}))
    asyncio.run(orch.handle({"capability": "memory:store",
                             "payload": {"content": "第二条", "session_id": "s1"}}))
    r = asyncio.run(orch.handle({"capability": "memory:get_history",
                                 "payload": {"session_id": "s1"}}))
    contents = [e["content"] for e in r["data"]["history"]]
    assert contents == ["第二条", "第一条"]  # 新→旧排序


def test_consolidate_moves_to_long_term(tmp_path):
    orch, bus = _make(tmp_path)
    seen = {}
    bus.subscribe(MEMORY_CONSOLIDATED, lambda event, **kw: seen.update(kw))
    asyncio.run(orch.handle({"capability": "memory:store",
                             "payload": {"content": "长期化内容", "session_id": "s1"}}))
    r = asyncio.run(orch.handle({"capability": "memory:consolidate",
                                 "payload": {"session_id": "s1"}}))
    assert r["data"]["consolidated"] == 1
    assert seen["count"] == 1
    assert orch._long.count() == 1  # 已固化到 SQLite


def test_retrieve_from_long_term_after_consolidate(tmp_path):
    orch, _ = _make(tmp_path)
    asyncio.run(orch.handle({"capability": "memory:store",
                             "payload": {"content": "喜欢看推理小说", "session_id": "s1"}}))
    asyncio.run(orch.handle({"capability": "memory:consolidate",
                             "payload": {"session_id": "s1"}}))
    r = asyncio.run(orch.handle({"capability": "memory:retrieve",
                                 "payload": {"query": "推理", "k": 5}}))
    assert any("推理小说" in m["content"] for m in r["data"]["memories"])


def test_unknown_capability(tmp_path):
    orch, _ = _make(tmp_path)
    r = asyncio.run(orch.handle({"capability": "memory:unknown", "payload": {}}))
    assert r["ok"] is False
