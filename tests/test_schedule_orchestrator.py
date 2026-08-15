"""test_schedule_orchestrator.py — 日程调度官单测（P0 补迁）"""
import asyncio
import json
import tempfile
import time
from pathlib import Path

from src.orchestrators.schedule_orchestrator import registry
from src.orchestrators.schedule_orchestrator.schedule_orchestrator import (
    CHECK_INTERVAL,
    CronExpr,
    ScheduleOrchestrator,
)
from src.shared.event_bus import EventBus
from src.shared.events import SCHEDULE_FIRED


def _make(cfg=None):
    bus = EventBus()
    bus.reset()
    d = tempfile.mkdtemp()
    cfg = dict(cfg or {})
    cfg.setdefault("data_file", str(Path(d) / "schedule.json"))
    orch = ScheduleOrchestrator(event_bus=bus, config=cfg)
    orch.start()
    return orch, bus


# ---------- CronExpr ----------

def test_cron_parse_and_match():
    assert CronExpr("* * * * *").matches(time.time())  # 每分钟匹配
    assert not CronExpr("0 0 1 1 *").matches(time.time())  # 元旦 00:00 一般不匹配
    assert CronExpr("*/5 * * * *")._sets["minute"] == set(range(0, 60, 5))


def test_cron_invalid_expr_raises():
    import pytest
    with pytest.raises(ValueError):
        CronExpr("not-a-cron")
    with pytest.raises(ValueError):
        CronExpr("* * *")  # 不足 5 段


# ---------- 能力 ----------

def test_capabilities_from_registry():
    orch, _ = _make()
    assert orch.capabilities() == registry.capabilities()
    assert "schedule:add" in orch.capabilities()


def test_add_list_remove_status_flow():
    """增删查：add → list → remove → status。"""
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "schedule:add",
                                 "payload": {"id": "s1", "cron": "0 9 * * *",
                                             "action": "stream:start",
                                             "payload": {"source_type": "video"},
                                             "title": "每日九点开播"}}))
    assert r["ok"] is True
    r = asyncio.run(orch.handle({"capability": "schedule:list", "payload": {}}))
    assert len(r["data"]["jobs"]) == 1 and r["data"]["jobs"][0]["action"] == "stream:start"
    r = asyncio.run(orch.handle({"capability": "schedule:status", "payload": {}}))
    assert r["data"]["job_count"] == 1 and r["data"]["enabled"] is True
    r = asyncio.run(orch.handle({"capability": "schedule:remove",
                                 "payload": {"id": "s1"}}))
    assert r["ok"] is True
    assert len(asyncio.run(orch.handle({"capability": "schedule:list",
                                        "payload": {}}))["data"]["jobs"]) == 0


def test_add_validation_errors():
    """非法 cron / 重复 id / 不存在 remove → 明确错误。"""
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "schedule:add",
                                 "payload": {"id": "s1", "cron": "bad",
                                             "action": "x"}}))
    assert r["ok"] is False and "cron" in r["error"]
    r = asyncio.run(orch.handle({"capability": "schedule:add",
                                 "payload": {"id": "s1", "cron": "* * * * *",
                                             "action": "x"}}))
    assert r["ok"] is True
    r = asyncio.run(orch.handle({"capability": "schedule:add",
                                 "payload": {"id": "s1", "cron": "* * * * *",
                                             "action": "x"}}))
    assert r["ok"] is False and "已存在" in r["error"]
    r = asyncio.run(orch.handle({"capability": "schedule:remove",
                                 "payload": {"id": "nope"}}))
    assert r["ok"] is False and "不存在" in r["error"]


# ---------- 触发 ----------

def test_tick_fires_event():
    """cron 匹配当前分钟 → _tick 发布 schedule:fired（action/payload 透传）。"""
    orch, bus = _make()
    fired = []
    bus.subscribe(SCHEDULE_FIRED, lambda event, **kw: fired.append(kw))
    # 当前分钟匹配的 cron
    import datetime
    now = datetime.datetime.now()
    cron = "{} {} * * *".format(now.minute, now.hour)
    asyncio.run(orch.handle({"capability": "schedule:add",
                             "payload": {"id": "t1", "cron": cron,
                                         "action": "live2d:expression",
                                         "payload": {"name": "开心"}}}))
    orch._tick()
    assert fired, "排期到点应发布 schedule:fired"
    assert fired[0]["action"] == "live2d:expression"
    assert fired[0]["payload"]["name"] == "开心"
    assert fired[0]["schedule_id"] == "t1"


def test_tick_skips_non_matching():
    """cron 不匹配 → 不发布事件。"""
    orch, bus = _make()
    fired = []
    bus.subscribe(SCHEDULE_FIRED, lambda event, **kw: fired.append(kw))
    asyncio.run(orch.handle({"capability": "schedule:add",
                             "payload": {"id": "t2", "cron": "0 0 1 1 *",
                                         "action": "x", "payload": {}}}))
    orch._tick()
    assert not fired


# ---------- 持久化 ----------

def test_persist_roundtrip():
    """运行时添加的排期落盘 → 重新构造加载。"""
    orch, _ = _make()
    data_file = orch._data_file
    asyncio.run(orch.handle({"capability": "schedule:add",
                             "payload": {"id": "p1", "cron": "0 12 * * 1",
                                         "action": "stream:stop"}}))
    orch.stop()
    assert data_file.exists()
    orch2, _ = _make({"data_file": str(data_file)})
    jobs = asyncio.run(orch2.handle({"capability": "schedule:list",
                                     "payload": {}}))["data"]["jobs"]
    assert len(jobs) == 1 and jobs[0]["id"] == "p1"


def test_disabled_no_thread():
    """enabled=false：检查线程不启动（能力仍可用）。"""
    orch, bus = _make({"enabled": False})
    assert orch._thread is None
    r = asyncio.run(orch.handle({"capability": "schedule:status", "payload": {}}))
    assert r["data"]["enabled"] is True  # start 已标记（生命周期角度）
    assert orch._thread is None