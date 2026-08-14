"""test_live_intelligence_orchestrator.py — P1 直播间智能调度官测试

覆盖 7 个精细子模块 + 调度官接线 + app 装配。
"""
import asyncio
import time

import pytest

from src.orchestrators.live_intelligence_orchestrator import LiveIntelligenceOrchestrator
from src.orchestrators.live_intelligence_orchestrator.commentary_policy import VNCommentaryPolicy
from src.orchestrators.live_intelligence_orchestrator.context_aggregator import ContextAggregator
from src.orchestrators.live_intelligence_orchestrator.danmaku_pool import DanmakuPool
from src.orchestrators.live_intelligence_orchestrator.danmaku_reactor import DanmakuReactor
from src.orchestrators.live_intelligence_orchestrator.heat_tracker import HeatTracker
from src.orchestrators.live_intelligence_orchestrator.pace import VNPaceController
from src.orchestrators.live_intelligence_orchestrator.speech_queue import SpeechQueue
from src.shared.event_bus import EventBus


def _run(coro):
    return asyncio.run(coro)


# ---------- DanmakuPool ----------

class TestDanmakuPool:
    def test_add_and_pending(self):
        pool = DanmakuPool(max_size=100, ttl=600)
        pool.add("你好", user="u1")
        pool.add("？怎么玩", user="u2")
        pending = pool.get_pending(10)
        assert len(pending) == 2
        # 权重高的问句在前
        assert pending[0]["text"] == "？怎么玩"
        assert pending[0]["weight"] > pending[1]["weight"]

    def test_mark_processed(self):
        pool = DanmakuPool()
        dm = pool.add("测试")
        assert pool.mark_processed([dm["id"]]) == 1
        assert pool.get_pending(10) == []

    def test_clear_and_size(self):
        pool = DanmakuPool()
        pool.add("a")
        pool.add("b")
        assert pool.size() == 2
        assert pool.clear() == 2
        assert pool.size() == 0

    def test_empty_text_raises(self):
        pool = DanmakuPool()
        with pytest.raises(ValueError):
            pool.add("   ")

    def test_pooled_event_published(self):
        bus = EventBus()
        pool = DanmakuPool(event_bus=bus)
        seen = []
        bus.subscribe("danmaku:pooled", lambda e="", **k: seen.append(k))
        pool.add("弹幕")
        assert len(seen) == 1
        assert seen[0]["text"] == "弹幕"


# ---------- DanmakuReactor ----------

class TestDanmakuReactor:
    def test_react_returns_reply(self):
        reactor = DanmakuReactor(global_cooldown=0, rate_max=100)
        reply = reactor.react("谢谢主播", user="u")
        assert reply in sum(reactor.DEFAULT_REACTIONS.values(), [])

    def test_question_sentiment(self):
        reactor = DanmakuReactor(global_cooldown=0, rate_max=100)
        assert reactor.react("怎么玩？", user="u") is not None

    def test_global_cooldown_blocks_second(self):
        reactor = DanmakuReactor(global_cooldown=100, rate_max=100)
        assert reactor.react("第一条") is not None
        assert reactor.react("第二条") is None

    def test_rate_limit(self):
        reactor = DanmakuReactor(global_cooldown=0, rate_max=2)
        reactor.react("a", user="u")
        reactor.react("b", user="u")
        assert reactor.react("c", user="u") is None

    def test_register_reaction(self):
        reactor = DanmakuReactor(global_cooldown=0)
        reactor.register_reaction("neutral", ["自定义回复"])
        reactor._next_available = {}
        reactor._global_next = 0
        reactor._last_reply = ""
        # "好好好" 无问句特征 → 中性情感 → 命中自定义 neutral 模板
        assert reactor.react("好好好", user="u") == "自定义回复"

    def test_reacted_event_published(self):
        bus = EventBus()
        reactor = DanmakuReactor(event_bus=bus, global_cooldown=0, rate_max=100)
        seen = []
        bus.subscribe("danmaku:reacted", lambda e="", **k: seen.append(k))
        reactor.react("好耶", user="u")
        assert len(seen) == 1
        assert seen[0]["reply"]


# ---------- HeatTracker ----------

class TestHeatTracker:
    def test_record_and_level(self):
        ht = HeatTracker(decay_per_sec=0, event_decay=1.0)
        ht.record_event("gift", count=2)
        assert ht.get_score() >= 16.0
        assert ht.get_level() == "cool"  # 16 ≥ 5 → cool

    def test_high_score_boiling(self):
        ht = HeatTracker(decay_per_sec=0, event_decay=1.0)
        for _ in range(15):
            ht.record_event("super_chat")
        assert ht.get_level() == "boiling"

    def test_time_decay(self):
        ht = HeatTracker(decay_per_sec=0.5, event_decay=1.0)
        ht.record_event("danmaku")
        time.sleep(0.05)
        assert ht.get_score() < 1.0

    def test_stats(self):
        ht = HeatTracker()
        ht.record_event("danmaku")
        stats = ht.get_stats()
        assert stats["count"] >= 1
        assert "level" in stats

    def test_heat_updated_event(self):
        bus = EventBus()
        ht = HeatTracker(event_bus=bus)
        seen = []
        bus.subscribe("heat:updated", lambda e="", **k: seen.append(k))
        ht.record_event("like")
        assert len(seen) == 1


# ---------- SpeechQueue ----------

class TestSpeechQueue:
    def test_enqueue_dequeue(self):
        q = SpeechQueue(timeout=600)
        assert q.enqueue("yuki", "你好")
        items = q.dequeue(1)
        assert len(items) == 1
        assert items[0]["text"] == "你好"

    def test_priority_insert(self):
        q = SpeechQueue(timeout=600)
        q.enqueue("a", "low", priority=1)
        q.enqueue("b", "high", priority=10)
        assert q.dequeue(1)[0]["text"] == "high"

    def test_timeout_expiry(self):
        q = SpeechQueue(timeout=0.01)
        q.enqueue("a", "old")
        time.sleep(0.02)
        assert q.dequeue(1) == []
        assert q.get_stats()["expired"] >= 1

    def test_peek(self):
        q = SpeechQueue(timeout=600)
        q.enqueue("a", "x")
        assert len(q.peek(5)) == 1
        assert q.size() == 1

    def test_clear(self):
        q = SpeechQueue(timeout=600)
        q.enqueue("a", "x")
        q.enqueue("b", "y")
        assert q.clear() == 2


# ---------- ContextAggregator ----------

class TestContextAggregator:
    def test_role_snapshot(self):
        ag = ContextAggregator()
        snap = ag.get_snapshot(role="yuki", focus="role")
        assert snap.current_emotion == "calm"
        assert hasattr(snap, "recent_danmaku")

    def test_cache_hit(self):
        ag = ContextAggregator(cache_ttl=10)
        ag.get_snapshot()
        ag.get_snapshot()
        assert ag.get_status()["cache_hit_count"] >= 1

    def test_ops_context(self):
        ag = ContextAggregator()
        ops = ag.get_ops_context()
        assert ops["live_status"] == "offline"

    def test_invalid_focus(self):
        ag = ContextAggregator()
        with pytest.raises(ValueError):
            ag.get_snapshot(focus="bad")

    def test_danmaku_event_cache(self):
        bus = EventBus()
        ag = ContextAggregator(event_bus=bus)
        ag.start()
        bus.publish("danmaku:received", content="测试弹幕", user_name="u")
        snap = ag.get_snapshot(focus="role")
        assert any(d["content"] == "测试弹幕" for d in snap.recent_danmaku)
        ag.stop()

    def test_pull_module_health(self):
        class FakeMod:
            def get_status(self):
                return {"status": "ok", "running": True}
        ag = ContextAggregator(tool_registry=FakeMod())
        health, _ = ag._collect_module_health()
        assert health["tool_registry"] == "healthy"


# ---------- CommentaryPolicy ----------

class TestCommentaryPolicy:
    def test_generate_states(self):
        policy = VNCommentaryPolicy()
        for state in ("choice", "puzzle", "menu", "transition", "cg", "dialogue", "unknown"):
            text = policy.generate_commentary(state)
            assert text
            assert len(text) <= policy.max_words * 2

    def test_invalid_state(self):
        policy = VNCommentaryPolicy()
        with pytest.raises(ValueError):
            policy.generate_commentary("bogus")


# ---------- Pace ----------

class TestPace:
    def test_choice_always(self):
        pc = VNPaceController()
        d = pc.decide_state("choice")
        assert d.should_comment is True
        assert d.priority == 90

    def test_dialogue_cooldown(self):
        pc = VNPaceController(min_commentary_interval=1000)
        now = time.time()
        d1 = pc.decide_state("cg", now=now)
        assert d1.should_comment is True
        d2 = pc.decide_state("cg", now=now + 1)
        assert d2.should_comment is False

    def test_first_dialogue_commentary(self):
        pc = VNPaceController()
        d = pc.decide_state("dialogue", text="你好世界")
        assert d.should_comment is True
        assert d.reason == "first_dialogue_commentary"


# ---------- Orchestrator ----------

class TestLiveIntelligenceOrchestrator:
    def test_capabilities(self):
        orch = LiveIntelligenceOrchestrator(event_bus=EventBus())
        caps = orch.capabilities()
        assert "intel:danmaku_pool_add" in caps
        assert "intel:context_snapshot" in caps
        assert "intel:heat_record" in caps
        assert "intel:pace_decide" in caps

    def test_handle_danmaku_pool(self):
        orch = LiveIntelligenceOrchestrator(event_bus=EventBus())
        r = _run(orch.handle({"capability": "intel:danmaku_pool_add",
                              "payload": {"text": "你好"}}))
        assert r["ok"] is True
        assert r["data"]["danmaku"]["text"] == "你好"

    def test_handle_heat(self):
        orch = LiveIntelligenceOrchestrator(event_bus=EventBus())
        r = _run(orch.handle({"capability": "intel:heat_record",
                              "payload": {"event_type": "like"}}))
        assert r["ok"] is True
        assert r["data"]["score"] > 0

    def test_handle_speech(self):
        orch = LiveIntelligenceOrchestrator(event_bus=EventBus())
        r = _run(orch.handle({"capability": "intel:speech_enqueue",
                              "payload": {"char_id": "yuki", "text": "hi"}}))
        assert r["ok"] is True
        assert r["data"]["queued"] is True

    def test_handle_commentary(self):
        orch = LiveIntelligenceOrchestrator(event_bus=EventBus())
        r = _run(orch.handle({"capability": "intel:commentary_generate",
                              "payload": {"state": "choice"}}))
        assert r["ok"] is True
        assert "选项" in r["data"]["commentary"]

    def test_handle_pace(self):
        orch = LiveIntelligenceOrchestrator(event_bus=EventBus())
        r = _run(orch.handle({"capability": "intel:pace_decide",
                              "payload": {"state": "menu"}}))
        assert r["ok"] is True
        assert r["data"]["should_comment"] is True

    def test_handle_context(self):
        orch = LiveIntelligenceOrchestrator(event_bus=EventBus())
        r = _run(orch.handle({"capability": "intel:context_snapshot",
                              "payload": {"focus": "role"}}))
        assert r["ok"] is True
        assert "recent_danmaku" in r["data"]["snapshot"]

    def test_handle_unknown(self):
        orch = LiveIntelligenceOrchestrator(event_bus=EventBus())
        r = _run(orch.handle({"capability": "intel:nope", "payload": {}}))
        assert r["ok"] is False

    def test_start_stop_health(self):
        orch = LiveIntelligenceOrchestrator(event_bus=EventBus())
        orch.start()
        assert orch.health()["status"] == "ok"
        orch.stop()
        assert orch.health()["status"] == "down"

    def test_app_boot_wiring(self, monkeypatch):
        for k, v in {
            "OPENAI_API_KEY": "sk-test", "OPENAI_BASE_URL": "https://x/v1",
            "OPENAI_MODEL": "gpt-test", "ZHIPU_API_KEY": "z-test",
            "ZHIPU_MODEL": "glm-test", "DASHSCOPE_API_KEY": "d-test",
            "WUSOUND_API_KEY": "w-test", "BILIBILI_ACCESS_KEY_ID": "b-id",
            "BILIBILI_ACCESS_KEY_SECRET": "b-secret", "BILIBILI_COOKIE": "b-cookie",
            "OBS_WS_PASSWORD": "obs-test",
        }.items():
            monkeypatch.setenv(k, v)
        from src.app import build_app_context
        app, _ = build_app_context()
        ctx = app.config["APP_CONTEXT"]
        names = {o.name for o in ctx["registry"].all()}
        assert "intelligence" in names