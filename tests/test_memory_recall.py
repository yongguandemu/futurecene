"""test_memory_recall.py — 分层检索集成单测（memory:recall / compress / review，任务四）

覆盖：strength→k 映射、L1/L2/L3 混合检索、压缩与审阅能力、现有行为回归。
"""
import asyncio

from src.orchestrators.memory_orchestrator.memory_orchestrator import MemoryOrchestrator
from src.shared.event_bus import EventBus
from src.shared.events import (
    DANMAKU_RECEIVED,
    LLM_RESPONDED,
    MEMORY_CONSOLIDATED,
    MEMORY_STORED,
)


def _make(tmp_path):
    bus = EventBus()
    bus.reset()
    orch = MemoryOrchestrator(event_bus=bus, db_path=str(tmp_path / "m.db"),
                              switch_check=lambda name: True)
    orch.start()
    return orch, bus


def _recall(orch, **payload):
    return asyncio.run(orch.handle({"capability": "memory:recall",
                                    "payload": payload}))


# ---------- strength → k 映射 ----------

def test_strength_k_mapping(tmp_path):
    orch, _ = _make(tmp_path)
    for strength, expected_k in [("low", 2), ("medium", 5), ("high", 10), ("ultra", 15)]:
        r = _recall(orch, strength=strength)
        assert r["ok"] is True and r["data"]["k"] == expected_k
    # 未知档回退默认（medium=5）
    r = _recall(orch, strength="bogus")
    assert r["data"]["k"] == 5
    orch.stop()


def test_recall_empty_state(tmp_path):
    orch, _ = _make(tmp_path)
    r = _recall(orch)
    assert r["ok"] is True
    assert r["data"]["memories"] == []
    assert r["data"]["source"] == {"l1": 0, "l2": 0, "l3": 0}
    orch.stop()


# ---------- L1/L2/L3 混合检索 ----------

def test_recall_hybrid_from_three_layers(tmp_path):
    orch, bus = _make(tmp_path)
    # L1：事件采集（弹幕 + 发言）
    bus.publish(DANMAKU_RECEIVED, content="观众喜欢推理小说")
    bus.publish(LLM_RESPONDED, capability="llm:chat", text="我也喜欢推理")
    # L2：中期摘要
    orch._mid.store("default", "中期摘要：观众偏好推理与悬疑", ["e1"])
    # L3：长期记忆
    orch._long.store(content="长期记忆：观众多次提到推理小说", role="user",
                     session_id="default")
    r = _recall(orch, query="推理", strength="high")
    assert r["ok"] is True
    assert r["data"]["k"] == 10
    contents = [m["content"] for m in r["data"]["memories"]]
    assert any("推理" in c for c in contents)
    source = r["data"]["source"]
    assert source["l1"] >= 1 and source["l2"] >= 1 and source["l3"] >= 1
    orch.stop()


def test_recall_no_query_returns_recent(tmp_path):
    orch, bus = _make(tmp_path)
    bus.publish(DANMAKU_RECEIVED, content="最近一条弹幕")
    orch._mid.store("default", "旧摘要", ["e1"])
    r = _recall(orch, strength="low")
    assert len(r["data"]["memories"]) == 2  # L1 1 条 + L2 1 条，k=2
    assert "最近一条弹幕" in r["data"]["memories"][0]["content"]  # 新→旧
    orch.stop()


def test_recall_character_bucket_isolation(tmp_path):
    orch, bus = _make(tmp_path)
    orch._mid.store("s1:yuki", "Yuki 的摘要", ["e1"])
    orch._mid.store("s1:lilith", "Lilith 的摘要", ["e2"])
    r1 = _recall(orch, query="摘要", session_id="s1", character_id="yuki")
    texts = [m["content"] for m in r1["data"]["memories"]]
    assert any("Yuki" in t for t in texts)
    assert not any("Lilith" in t for t in texts)
    orch.stop()


# ---------- memory:compress（手动） ----------

def test_manual_compress_triggered(tmp_path):
    orch, bus = _make(tmp_path)
    # L1 写入足够文本（通过 memory:store 事件采集不到，直接注入 logger 缓冲）
    for i in range(10):
        bus.publish(DANMAKU_RECEIVED, content="长弹幕内容" * 100)
    r = asyncio.run(orch.handle({"capability": "memory:compress",
                                 "payload": {"session_id": "s1"}}))
    assert r["ok"] is True
    assert r["data"]["triggered"] is True
    assert orch._mid.count("s1") >= 1
    orch.stop()


def test_manual_compress_switch_off(tmp_path):
    bus = EventBus()
    bus.reset()
    orch = MemoryOrchestrator(event_bus=bus, db_path=str(tmp_path / "m.db"),
                              switch_check=lambda name: name != "memory_compression")
    orch.start()
    r = asyncio.run(orch.handle({"capability": "memory:compress", "payload": {}}))
    assert r["ok"] is False
    assert "开关" in (r["error"] or "")
    orch.stop()


# ---------- memory:review ----------

def test_review_propose_and_resolve(tmp_path):
    orch, _ = _make(tmp_path)
    r = asyncio.run(orch.handle({"capability": "memory:review", "payload": {
        "action": "propose", "source_memory_id": "l1",
        "content": "观众高频话题：推理", "reason": "出现 10 次"}}))
    assert r["ok"] is True and r["data"]["proposal_id"]
    r2 = asyncio.run(orch.handle({"capability": "memory:review",
                                  "payload": {"action": "list", "status": "pending"}}))
    assert len(r2["data"]["proposals"]) == 1
    pid = r2["data"]["proposals"][0]["proposal_id"]
    r3 = asyncio.run(orch.handle({"capability": "memory:review", "payload": {
        "action": "accept", "proposal_id": pid}}))
    assert r3["data"]["accepted"] is True
    orch.stop()


def test_review_unknown_action(tmp_path):
    orch, _ = _make(tmp_path)
    r = asyncio.run(orch.handle({"capability": "memory:review",
                                 "payload": {"action": "bogus"}}))
    assert r["ok"] is False
    orch.stop()


# ---------- 现有行为回归（规格书 4.6：不影响现有功能） ----------

def test_existing_store_retrieve_unchanged(tmp_path):
    orch, _ = _make(tmp_path)
    r1 = asyncio.run(orch.handle({"capability": "memory:store",
                                  "payload": {"content": "观众喜欢看VN", "session_id": "s1"}}))
    assert r1["ok"] is True
    r2 = asyncio.run(orch.handle({"capability": "memory:retrieve",
                                  "payload": {"query": "VN", "session_id": "s1", "k": 3}}))
    assert r2["data"]["memories"][0]["content"] == "观众喜欢看VN"
    orch.stop()


def test_existing_consolidate_unchanged(tmp_path):
    orch, bus = _make(tmp_path)
    seen = {}
    bus.subscribe(MEMORY_CONSOLIDATED, lambda event, **kw: seen.update(kw))
    asyncio.run(orch.handle({"capability": "memory:store",
                             "payload": {"content": "长期化内容", "session_id": "s1"}}))
    r = asyncio.run(orch.handle({"capability": "memory:consolidate",
                                 "payload": {"session_id": "s1"}}))
    assert r["data"]["consolidated"] == 1
    assert seen["count"] == 1
    assert orch._long.count() == 1
    orch.stop()


def test_capabilities_extended(tmp_path):
    orch, _ = _make(tmp_path)
    caps = orch.capabilities()
    for expected in ("memory:store", "memory:recall", "memory:compress", "memory:review"):
        assert expected in caps
    assert len(caps) == 7
    orch.stop()


def test_memory_stored_event_published(tmp_path):
    orch, bus = _make(tmp_path)
    seen = {}
    bus.subscribe(MEMORY_STORED, lambda event, **kw: seen.update(kw))
    asyncio.run(orch.handle({"capability": "memory:store",
                             "payload": {"content": "事件发布", "session_id": "s1"}}))
    assert seen["session_id"] == "s1"
    orch.stop()
