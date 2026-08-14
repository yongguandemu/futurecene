"""test_experience_orchestrator.py — 游戏经验学习调度官单测（mock 适配器/临时仓库）"""
import asyncio

from src.orchestrators.experience_orchestrator import registry
from src.orchestrators.experience_orchestrator.experience_orchestrator import (
    ExperienceOrchestrator,
)
from src.orchestrators.experience_orchestrator.learn_brain import ExperienceLearnBrain
from src.shared.event_bus import EventBus


class FakeAdapter:
    def __init__(self):
        self.last_scene = {"state": {"scene_type": "menu", "text": "开始"}}
        self.feedback_from_heartbeat = False
        self.operations = []

    def _push_operation(self, *args, **kwargs):
        self.operations.append(args)


def _make(adapter=None, cfg=None):
    bus = EventBus()
    bus.reset()
    cfg = cfg or {"game": "", "data_file": ":memory:",
                  "planner_enabled": False, "curriculum_enabled": False,
                  "operation_check_interval": 0.05}
    orch = ExperienceOrchestrator(event_bus=bus, config=cfg,
                                   adapter=adapter or FakeAdapter())
    orch.start()
    return orch, bus


def test_capabilities_from_registry():
    orch, _ = _make()
    caps = orch.capabilities()
    assert caps == registry.capabilities()
    assert "experience:decide" in caps and "experience:feedback" in caps


def test_decide_ok():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "experience:decide",
                                 "payload": {"state": {"scene_type": "menu",
                                                       "text": "开始"}}}))
    assert r["ok"] is True and r["data"]["decided"] is True
    orch.stop()


def test_feedback_ok():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "experience:feedback",
                                 "payload": {"state_changed": False,
                                             "event_positive": True,
                                             "error_context": ""}}))
    assert r["ok"] is True
    orch.stop()


def test_stats_and_knowledge():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "experience:stats",
                                 "payload": {}}))
    assert "entries" in r["data"]
    r = asyncio.run(orch.handle({"capability": "experience:knowledge",
                                 "payload": {}}))
    assert "games" in r["data"]
    orch.stop()


def test_inject_task_and_plan():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "experience:inject_task",
                                 "payload": {"goal": "找到村庄"}}))
    assert r["ok"] is True
    orch.stop()


def test_decide_before_start():
    orch, _ = _make()
    orch.stop()  # 停掉 brain
    orch._brain._thread = None
    orch._started = False
    orch._brain = None  # 模拟未启动
    r = asyncio.run(orch.handle({"capability": "experience:decide",
                                 "payload": {"state": {}}}))
    assert r["ok"] is False  # brain 未启动
    orch.stop()


def test_unknown_capability():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "experience:unknown",
                                 "payload": {}}))
    assert r["ok"] is False
    orch.stop()


def test_health():
    orch, _ = _make()
    assert orch.health()["status"] == "ok"
    orch.stop()


def test_experience_recorded_event(tmp_path):
    bus = EventBus()
    bus.reset()
    adapter = FakeAdapter()
    cfg = {"game": "", "data_file": str(tmp_path / "exp.json"),
           "planner_enabled": False, "curriculum_enabled": False,
           "operation_check_interval": 0.05}
    orch = ExperienceOrchestrator(event_bus=bus, config=cfg, adapter=adapter)
    orch.start()
    # 触发一次决策 + 一次反馈（回写经验库）
    asyncio.run(orch.handle({"capability": "experience:decide",
                             "payload": {"state": {"scene_type": "menu",
                                                   "text": "开始"}}}))
    asyncio.run(orch.handle({"capability": "experience:feedback",
                             "payload": {"state_changed": True,
                                         "event_positive": True,
                                         "error_context": ""}}))
    assert orch._brain is not None
    orch.stop()
    assert orch._started is False