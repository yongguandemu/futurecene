"""test_speech_scheduler.py — 发言时间线调度（队列/空档/过期/覆盖/插入）"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.commander.speech_scheduler import SpeechScheduler

PLAN = {"text": "大家今天过得怎么样？", "mood": "calm",
        "suggested_window_sec": 90, "duration_estimate": 6.0}


def _scheduler(**kw):
    return SpeechScheduler(**kw)


def test_submit_batch_and_tick_schedules_one():
    """主动批入队 3 条 → 每次 tick 出 1 条（队列剩余）。"""
    s = _scheduler()
    assert s.submit_batch([PLAN] * 3, role="yuki") == 3
    events = s.tick()
    assert len(events) == 1 and events[0]["event"] == "scheduled"
    assert s.snapshot()["playing"]["kind"] == "active"
    assert s.snapshot()["queue_size"] == 2


def test_batch_mode_off_ignores_batch():
    """batch_mode 开关关 → 主动批忽略（返回 0）。"""
    s = _scheduler(switch_check=lambda n: n != "batch_mode")
    assert s.submit_batch([PLAN]) == 0
    assert s.tick() == []


def test_passive_insert_plays_immediately_when_idle():
    """空闲时被动发言直接播放（INSERTED + SCHEDULED 事件）。"""
    events = []
    s = _scheduler()
    result = s.submit_passive("主播今天好棒！", role="yuki")
    assert result["queued"] is False
    assert s.snapshot()["playing"]["kind"] == "passive"


def test_passive_queued_when_busy_no_preempt():
    """正在播放时被动发言排队不抢占。"""
    s = _scheduler()
    s.submit_batch([PLAN])
    s.tick()  # active 开播（busy）
    r = s.submit_passive("插播消息")
    assert r["queued"] is True
    snap = s.snapshot()
    assert snap["playing"]["kind"] == "active"  # 未被抢占
    assert snap["queue_size"] == 1


def test_complete_releases_slot_and_next_plays():
    """complete 释放空档 → tick 出下一条。"""
    s = _scheduler()
    s.submit_batch([PLAN, PLAN])
    s.tick()
    uid = s.snapshot()["playing"]["uid"]
    s.complete(uid)
    events = s.tick()
    assert events and events[0]["event"] == "scheduled"


def test_expired_items_dropped():
    """超时未播放（active_ttl）→ 丢弃 expired。"""
    s = _scheduler(active_ttl=5.0)
    s.submit_batch([PLAN])
    now = time.time()
    events = s.tick(now + 10.0)  # 模拟 10 秒后调度
    assert events and events[0]["reason"] == "expired"
    assert s.snapshot()["queue_size"] == 0


def test_covered_active_dropped():
    """排队中的主动项被被动发言推迟 ≥3 次 → 丢弃 covered。"""
    s = _scheduler()
    s.submit_batch([PLAN] * 3)        # 3 条 active 入队
    s.tick()                          # 第 1 条开播（队列剩 2 条 active）
    for i in range(3):
        s.submit_passive(f"插播{i}")   # 3 次被动插入 → 队列 active 各 miss+3
    s.complete(s.snapshot()["playing"]["uid"])
    events = s.tick()
    dropped = [e for e in events if e.get("reason") == "covered"]
    assert dropped  # 队首 active 已被覆盖丢弃
    # 丢弃后继续出队下一条（主动或被动），空档不空转
    assert s.snapshot()["playing"] is not None


def test_real_time_mode_off_queues_passive():
    """real_time_mode 关 → 被动发言空闲也排队（不直接播）。"""
    s = _scheduler(switch_check=lambda n: n != "real_time_mode")
    r = s.submit_passive("排队消息")
    assert r["queued"] is True
    assert s.snapshot()["playing"] is None
    assert s.snapshot()["queue_size"] == 1


def test_submit_batch_skips_empty_text():
    """空文本计划不入队。"""
    s = _scheduler()
    assert s.submit_batch([{}, {"text": "  "}, PLAN]) == 1
    assert s.snapshot()["queue_size"] == 1


def test_scheduled_event_reaches_pipeline_and_completes(monkeypatch):
    """端到端闭环：scheduler 出队 → speech:scheduled → pipeline 播放前合成 → complete 回执。"""
    from src.commander.danmaku_pipeline import DanmakuPipeline
    from src.shared.event_bus import EventBus
    bus = EventBus()
    s = _scheduler(event_bus=bus)
    pipe = DanmakuPipeline(event_bus=bus)
    pipe.set_speech_scheduler(s)
    pipe.start()
    called = {}

    async def fake_speak(text, role, uid=""):
        called["text"] = text
        pipe._complete_speech(uid)

    monkeypatch.setattr(pipe, "_speak_scheduled", fake_speak)
    s.submit_batch([PLAN])
    s.tick()  # 出队 → 发布 speech:scheduled → pipeline 消费 → complete
    assert called.get("text") == PLAN["text"]
    assert s.snapshot()["playing"] is None  # 空档已释放
    pipe.stop()
