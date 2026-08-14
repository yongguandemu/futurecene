"""test_safety_orchestrator.py — 安全调度官单测（关键词拦截，规则兜底）"""
import asyncio

from src.orchestrators.safety_orchestrator import registry
from src.orchestrators.safety_orchestrator.safety_orchestrator import SafetyOrchestrator
from src.shared.event_bus import EventBus
from src.shared.events import SAFETY_BLOCKED, SAFETY_FLAGGED


def _make(tmp_path):
    bus = EventBus()
    bus.reset()
    return SafetyOrchestrator(event_bus=bus, rules_file="", model_dir=""), bus


def test_capabilities_from_registry(tmp_path):
    orch, _ = _make(tmp_path)
    assert orch.capabilities() == registry.capabilities() == [
        "safety:check_input", "safety:check_output", "safety:reload_rules",
    ]


def test_block_sensitive_word(tmp_path):
    orch, bus = _make(tmp_path)
    seen = {}
    bus.subscribe(SAFETY_BLOCKED, lambda event, **kw: seen.update(kw))
    r = asyncio.run(orch.handle({"capability": "safety:check_input",
                                 "payload": {"text": "我们来讨论赌博吧"}}))
    assert r["data"]["verdict"] == "block"
    assert "赌博" in r["data"]["reason"]
    assert seen["matched"] == ["赌博"]


def test_flag_word(tmp_path):
    orch, bus = _make(tmp_path)
    seen = {}
    bus.subscribe(SAFETY_FLAGGED, lambda event, **kw: seen.update(kw))
    r = asyncio.run(orch.handle({"capability": "safety:check_output",
                                 "payload": {"text": "这个话题有点敏感"}}))
    assert r["data"]["verdict"] == "flag"
    assert seen["matched"] == ["敏感"]


def test_allow_normal_text(tmp_path):
    orch, _ = _make(tmp_path)
    r = asyncio.run(orch.handle({"capability": "safety:check_input",
                                 "payload": {"text": "今天天气真好"}}))
    assert r["data"]["verdict"] == "allow"


def test_reload_rules(tmp_path):
    orch, _ = _make(tmp_path)
    r = asyncio.run(orch.handle({"capability": "safety:reload_rules", "payload": {}}))
    assert r["ok"] is True
    assert r["data"]["loaded"] > 0


def test_unknown_capability(tmp_path):
    orch, _ = _make(tmp_path)
    r = asyncio.run(orch.handle({"capability": "safety:unknown", "payload": {}}))
    assert r["ok"] is False
