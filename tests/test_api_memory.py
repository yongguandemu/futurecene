"""test_api_memory.py — /api/memory + /api/memory/review 路由单测（任务五 5.4）"""
import asyncio

from src.orchestrators.memory_orchestrator.memory_orchestrator import MemoryOrchestrator
from src.shared.event_bus import EventBus
from src.shared.events import DANMAKU_RECEIVED
from src.web.app_factory import create_app


def _make_app(tmp_path):
    bus = EventBus()
    bus.reset()
    orch = MemoryOrchestrator(event_bus=bus, db_path=str(tmp_path / "m.db"),
                              switch_check=lambda name: True)
    orch.start()
    app = create_app({"memory": orch, "event_bus": bus})
    return app, orch, bus


def test_memory_overview(tmp_path):
    app, orch, bus = _make_app(tmp_path)
    bus.publish(DANMAKU_RECEIVED, content="观众喜欢推理")
    r = app.test_client().get("/api/memory")
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["l1"]["count"] >= 1
    assert data["l2"]["count"] == 0
    assert data["l3"]["count"] == 0
    orch.stop()


def test_memory_recall_endpoint(tmp_path):
    app, orch, bus = _make_app(tmp_path)
    bus.publish(DANMAKU_RECEIVED, content="观众喜欢推理小说")
    r = app.test_client().get("/api/memory/recall",
                              query_string={"query": "推理", "strength": "high"})
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["k"] == 10
    assert any("推理" in m["content"] for m in data["memories"])
    orch.stop()


def test_memory_recall_strength_from_config(tmp_path):
    app, orch, bus = _make_app(tmp_path)
    r = app.test_client().get("/api/memory/recall")
    assert r.get_json()["data"]["k"] == 5  # 默认 medium
    orch.stop()


def test_review_propose_list_resolve(tmp_path):
    app, orch, _ = _make_app(tmp_path)
    client = app.test_client()
    # 生成提案
    r = asyncio.run(orch.handle({"capability": "memory:review", "payload": {
        "action": "propose", "source_memory_id": "l1",
        "content": "观众高频话题：推理", "reason": "出现多次"}}))
    pid = r["data"]["proposal_id"]
    # 列表
    r = client.get("/api/memory/review", query_string={"status": "pending"})
    assert r.status_code == 200
    proposals = r.get_json()["data"]["proposals"]
    assert len(proposals) == 1 and proposals[0]["proposal_id"] == pid
    # 接受
    r = client.post("/api/memory/review",
                    json={"action": "accept", "proposal_id": pid})
    assert r.status_code == 200
    assert r.get_json()["data"]["accepted"] is True
    # 已处置不可重复
    r = client.post("/api/memory/review",
                    json={"action": "reject", "proposal_id": pid})
    assert r.get_json()["data"]["rejected"] is False
    orch.stop()


def test_review_invalid_action_and_missing_id(tmp_path):
    app, orch, _ = _make_app(tmp_path)
    client = app.test_client()
    r = client.post("/api/memory/review", json={"action": "bogus", "proposal_id": "p1"})
    assert r.status_code == 400
    r = client.post("/api/memory/review", json={"action": "accept"})
    assert r.status_code == 400
    orch.stop()


def test_memory_routes_require_orchestrator(tmp_path):
    app = create_app({})
    assert app.test_client().get("/api/memory").status_code == 503
    assert app.test_client().get("/api/memory/recall").status_code == 503
