"""test_active_dialogue_batch.py — 主动对话批量路径（任务二）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.llm_orchestrator.active_dialogue import ActiveDialogue
from src.orchestrators.llm_orchestrator.batch_planner import BatchPlanner
from src.shared.event_bus import EventBus


def _dialogue(batch_mode=True, planner=None):
    bus = EventBus()
    ad = ActiveDialogue(event_bus=bus, config={
        "min_cooldown": 0, "max_silence": 99999, "trigger_probability": 1.0,
        "enabled": True})
    if planner is not None:
        ad.set_batch_planner(planner)
    ad.set_switch_check(lambda name: batch_mode if name == "batch_mode" else True)
    return ad, bus


def test_batch_mode_publishes_batch_ready():
    """batch_mode 开 → tick 走批量路径，发布 speech:batch_ready。"""
    received = []
    planner = BatchPlanner(chat_fn=lambda m: (
        '[{"text": "批一", "mood": "calm", "suggested_window_sec": 90, "duration_estimate": 5},'
        '{"text": "批二", "mood": "happy", "suggested_window_sec": 120, "duration_estimate": 6}]', {}))
    ad, bus = _dialogue(batch_mode=True, planner=planner)
    bus.subscribe("speech:batch_ready", lambda event, **kw: received.append(kw))
    result = ad.tick()
    assert result is not None and "batch" in result
    assert len(result["batch"]) == 2
    assert received and received[0]["count"] == 2


def test_batch_mode_off_keeps_single_path():
    """batch_mode 关 → 保持原单条路径（不发布 batch_ready）。"""
    received = []
    planner = BatchPlanner(chat_fn=lambda m: (
        '[{"text": "批一", "mood": "calm", "suggested_window_sec": 90, "duration_estimate": 5}]', {}))
    ad, bus = _dialogue(batch_mode=False, planner=planner)
    ad.set_generator(lambda: {"text": "单条话题", "mood": "happy"})
    bus.subscribe("speech:batch_ready", lambda event, **kw: received.append(kw))
    result = ad.tick()
    assert result == {"text": "单条话题", "mood": "happy"}
    assert received == []


def test_batch_planner_failure_falls_back_single():
    """批量生成失败 → 回退单条路径（不中断冷场闲聊）。"""
    class Boom:
        def generate(self, **kw):
            raise RuntimeError("planner down")
    ad, bus = _dialogue(batch_mode=True, planner=Boom())
    ad.set_generator(lambda: {"text": "兜底话题", "mood": "calm"})
    result = ad.tick()
    assert result == {"text": "兜底话题", "mood": "calm"}


def test_batch_planner_none_keeps_single():
    """未注入 BatchPlanner → 单条路径。"""
    ad, bus = _dialogue(batch_mode=True, planner=None)
    ad.set_generator(lambda: {"text": "无批量", "mood": "default"})
    result = ad.tick()
    assert result == {"text": "无批量", "mood": "default"}
