"""coordinator 单测（mock pipeline，不依赖真实 LLM/TTS）。

执行模型：发布线程只负责启动 collab-exec 执行线程；互斥由执行线程全程持有，
execute_with 完成后 finally 释放并排空待发队列。因此测试在发布事件后需轮询
pipeline.calls / 互斥状态等待执行线程完成（_wait_until）。
"""
import asyncio
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.shared.event_bus import EventBus
from src.shared.events import CHARACTER_PRESENCE_CHANGED
from src.orchestrators.collaboration.coordinator import CollaborationCoordinator


class FakeSession:
    """会话状态最小替身：提供 present_roles（在场模型单一来源，ADR-001）。"""

    def __init__(self, present_roles=None):
        self.present_roles = set(present_roles if present_roles is not None
                                 else {"yuki", "lilith"})


class FakeProfiles:
    def all_roles(self):
        return ["yuki", "lilith"]

    def keywords_for(self, role):
        return {"yuki": {"topics": ["故事"], "patterns": []},
                "lilith": {"topics": ["直播"], "patterns": []}}[role]

    def load(self, role):
        # 与 CharacterProfileLoader.load 接口对齐：返回带 system_prompt 的画像对象
        prompts = {"yuki": "你是Yuki酱，活泼元气的虚拟主播。",
                   "lilith": "你是Lilith，冷静知性的虚拟主播。"}
        return SimpleNamespace(system_prompt=prompts[role])


class FakePipeline:
    """实现执行器协议：async def execute_with(text, role, system_prompt, turn_context) -> dict。"""

    def __init__(self):
        self.calls = []

    async def execute_with(self, text, role, system_prompt="", turn_context=None):
        self.calls.append({"text": text, "role": role,
                           "prompt_has_persona": bool(system_prompt)})
        return {"ok": True, "data": {"reply": "ok", "audio_id": "a1"}}


class BlockingPipeline(FakePipeline):
    """首条调用阻塞，模拟长发言（验证互斥期间后续请求 deferred 排队）。"""

    def __init__(self):
        super().__init__()
        self._gate = threading.Event()

    async def execute_with(self, text, role, system_prompt="", turn_context=None):
        await super().execute_with(text, role, system_prompt, turn_context)
        if len(self.calls) == 1:
            self._gate.wait(timeout=2.0)   # 首条阻塞至 release_gate
        return {"ok": True, "data": {"reply": "ok", "audio_id": "a1"}}

    def release_gate(self):
        self._gate.set()


def _wait_until(predicate, timeout=2.0):
    """轮询等待执行线程完成（发布事件后异步执行，需轮询而非立即断言）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _make_coordinator(bus, pipeline=None, trigger_probability=0.0, session=None, **kw):
    return CollaborationCoordinator(bus, pipeline=pipeline,
                                    profiles=FakeProfiles(),
                                    session=session if session is not None
                                    else FakeSession(),
                                    trigger_probability=trigger_probability,
                                    **kw)


def test_danmaku_arbitrates_and_executes():
    bus = EventBus()
    pipeline = FakePipeline()
    co = _make_coordinator(bus, pipeline=pipeline, trigger_probability=0.0,
                           awareness_enabled=True)
    co.start()
    bus.publish("danmaku:received", content="@Lilith 你同意吗", user_name="观众甲")
    # 异步执行：轮询等待执行线程完成
    assert _wait_until(lambda: len(pipeline.calls) == 1), "执行线程未在超时内完成"
    assert pipeline.calls[0]["role"] == "lilith"
    assert pipeline.calls[0]["prompt_has_persona"] is True
    # 执行完成后互斥应已释放（无锁泄漏）
    assert _wait_until(lambda: co._tt.current_speaker is None), "互斥未释放"
    co.stop()


def test_speech_completed_triggers_banter():
    bus = EventBus()
    pipeline = FakePipeline()
    co = _make_coordinator(bus, pipeline=pipeline, trigger_probability=1.0,
                           trigger_global_cooldown=0.0, awareness_enabled=True)
    co.start()
    bus.publish("danmaku:received", content="讲个故事吧", user_name="观众甲")
    assert _wait_until(lambda: len(pipeline.calls) >= 1), "首轮执行未完成"
    # 模拟 pipeline 发言完成（真实链路中 execute_with 内部发布 speech:completed）
    spoke = pipeline.calls[0]["role"]
    bus.publish("speech:completed", role=spoke, text="回复内容", audio_id="a1")
    # 触发接话：冷却为 0 且概率 1 → 应出现接话执行
    assert _wait_until(lambda: len(pipeline.calls) >= 2), "接话未发生"
    co.stop()


def test_command_filtered():
    bus = EventBus()
    pipeline = FakePipeline()
    co = _make_coordinator(bus, pipeline=pipeline)
    co.start()
    bus.publish("danmaku:received", content="!stop 全体闭嘴", user_name="观众甲")
    time.sleep(0.2)   # 留足时间：若未被过滤则必然执行
    assert pipeline.calls == []
    co.stop()


def test_stop_unsubscribes():
    bus = EventBus()
    pipeline = FakePipeline()
    co = _make_coordinator(bus, pipeline=pipeline)
    co.start()
    co.stop()
    # 退订生效：stop() 后再发布 danmaku 不应执行
    bus.publish("danmaku:received", content="@Lilith 你好", user_name="观众甲")
    time.sleep(0.2)
    assert pipeline.calls == []


def test_pipeline_none_does_not_leak_mutex():
    bus = EventBus()
    co = _make_coordinator(bus, pipeline=None)
    co.start()
    # 仲裁已 acquire 互斥，但 pipeline 为 None：_execute 必须先 release 再跳过
    bus.publish("danmaku:received", content="@Lilith 你好", user_name="观众甲")
    assert _wait_until(lambda: co._tt.current_speaker is None), \
        "互斥泄漏（pipeline=None 未释放）"
    # 注入执行器后后续请求可正常放行（若互斥泄漏则会永远排队）
    pipeline = FakePipeline()
    co._pipeline = pipeline
    bus.publish("danmaku:received", content="@Lilith 再说一次", user_name="观众乙")
    assert _wait_until(lambda: len(pipeline.calls) == 1), "后续请求被互斥泄漏阻塞"
    co.stop()


def test_deferred_queue_drained_after_completion():
    bus = EventBus()
    pipeline = BlockingPipeline()
    co = _make_coordinator(bus, pipeline=pipeline)
    co.start()
    bus.publish("danmaku:received", content="@Lilith 第一句", user_name="观众甲")
    assert _wait_until(lambda: len(pipeline.calls) == 1), "首条执行未开始"
    # 首条仍在执行（互斥占用），第二条仲裁应 deferred 入队
    bus.publish("danmaku:received", content="@Lilith 第二句", user_name="观众乙")
    assert _wait_until(lambda: co._tt.pending_count() == 1), "第二条未入待发队列"
    assert pipeline.calls[-1]["text"] == "@Lilith 第一句"
    # 首条完成 → finally 释放互斥 → 排空队列 → 第二条自动执行
    pipeline.release_gate()
    assert _wait_until(lambda: len(pipeline.calls) == 2), "deferred 未在完成时排空"
    assert pipeline.calls[1]["text"] == "@Lilith 第二句"
    assert _wait_until(lambda: co._tt.current_speaker is None), "互斥未释放"
    co.stop()


def test_presence_change_syncs_single_source():
    """在场模型单一来源：session.present_roles 变化经 presence_changed 同步仲裁器与触发器。

    构造时在场名单取自 session（而非 profiles.all_roles() 静态全量）；session 变更
    事件驱动 set_present_roles + update_runtime(present_roles=...) 收敛，无分叉。
    """
    bus = EventBus()
    session = FakeSession(present_roles={"yuki", "lilith"})
    co = _make_coordinator(bus, pipeline=FakePipeline(), session=session)
    co.start()
    # 初始：仲裁器与触发器在场集 == session 在场集
    assert co._arb._present_roles() == {"yuki", "lilith"}
    assert co._triggers._present == {"yuki", "lilith"}
    # 离场 lilith → presence_changed → 双组件同步收敛为 {yuki}
    session.present_roles.discard("lilith")
    bus.publish(CHARACTER_PRESENCE_CHANGED, role="lilith", present=False,
                session_id="s")
    assert co._arb._present_roles() == {"yuki"}
    assert co._triggers._present == {"yuki"}
    # 进场 lilith → 再次同步回 {yuki, lilith}
    session.present_roles.add("lilith")
    bus.publish(CHARACTER_PRESENCE_CHANGED, role="lilith", present=True,
                session_id="s")
    assert co._arb._present_roles() == {"yuki", "lilith"}
    assert co._triggers._present == {"yuki", "lilith"}
    co.stop()
