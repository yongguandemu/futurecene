"""test_decision_log.py — 决策日志（规格书 5.6.4）

覆盖：
1. 记录/最近查询/统计
2. no_action 必须带 reason_code（「为何不回应」）
3. min_interval 周期抑制（周期心跳防刷屏）
4. attach 后发布 decision:logged 事件
5. 环形缓冲上限
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.shared.decision_log import (
    OUTCOME_BLOCKED, OUTCOME_EXECUTED, OUTCOME_NO_ACTION,
    DecisionLog, default_log, record_decision, clear_log, log_stats,
)
from src.shared.events import DECISION_LOGGED
from src.shared.event_bus import EventBus


def test_record_and_recent():
    log = DecisionLog()
    log.record("arbitrator", OUTCOME_NO_ACTION, "arbitrate_no_winner",
               layer="L2", capability="collab:arbitrate")
    log.record("arbitrator", OUTCOME_EXECUTED, "arbitrated",
               layer="L2", capability="collab:arbitrate")
    entries = log.recent(10)
    assert len(entries) == 2
    assert entries[0].outcome == OUTCOME_EXECUTED          # 最新在前
    assert entries[1].reason_code == "arbitrate_no_winner"


def test_no_action_carries_reason():
    log = DecisionLog()
    log.record("learn_brain", OUTCOME_NO_ACTION, "no_candidate_action",
               layer="L1", capability="experience:decide",
               detail="经验/规则均无候选")
    e = log.recent(1)[0]
    assert e.outcome == OUTCOME_NO_ACTION
    assert e.reason_code == "no_candidate_action"          # 为何不回应
    assert e.layer == "L1"
    assert e.to_dict()["detail"] == "经验/规则均无候选"


def test_stats():
    log = DecisionLog()
    log.record("a", OUTCOME_NO_ACTION, "r1")
    log.record("a", OUTCOME_NO_ACTION, "r1")
    log.record("a", OUTCOME_BLOCKED, "r2")
    stats = log.stats()
    assert stats["total"] == 3
    assert stats["by_outcome"][OUTCOME_NO_ACTION] == 2
    assert stats["by_reason_code"]["r1"] == 2


def test_min_interval_suppresses():
    log = DecisionLog()
    first = log.record("learn_brain", OUTCOME_NO_ACTION, "fuse_paused",
                       min_interval=60)
    second = log.record("learn_brain", OUTCOME_NO_ACTION, "fuse_paused",
                        min_interval=60)
    assert first is not None
    assert second is None                                   # 抑制期内重复记录被跳过
    assert len(log.recent(10)) == 1


def test_different_reason_not_suppressed():
    log = DecisionLog()
    log.record("learn_brain", OUTCOME_NO_ACTION, "fuse_paused", min_interval=60)
    assert log.record("learn_brain", OUTCOME_NO_ACTION, "no_candidate_action",
                      min_interval=60) is not None          # 不同 reason_code 不抑制


def test_ring_buffer_cap():
    log = DecisionLog(max_entries=3)
    for i in range(5):
        log.record("src", OUTCOME_EXECUTED, "r{}".format(i))
    entries = log.recent(100)
    assert len(entries) == 3                                 # 超限淘汰最旧
    assert entries[-1].reason_code == "r2"


def test_publishes_event_when_attached():
    bus = EventBus()
    bus.reset()
    got = []
    bus.subscribe(DECISION_LOGGED, lambda **kw: got.append(kw))
    log = DecisionLog(event_bus=bus)
    log.record("command_router", OUTCOME_BLOCKED, "orchestrator_disabled",
               layer="L1", capability="music:next", decision_id="abc123")
    assert got and got[0]["reason_code"] == "orchestrator_disabled"
    assert got[0]["decision_id"] == "abc123"
    assert got[0]["outcome"] == OUTCOME_BLOCKED


def test_module_level_default_log():
    clear_log()
    entry = record_decision("arbitrator", OUTCOME_NO_ACTION,
                            "arbitrate_no_winner", layer="L2",
                            capability="collab:arbitrate")
    assert entry is not None
    stats = log_stats()
    assert stats["by_outcome"][OUTCOME_NO_ACTION] == 1
    clear_log()
    assert log_stats()["total"] == 0
