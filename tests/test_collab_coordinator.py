"""coordinator 单测（mock pipeline，不依赖真实 LLM/TTS）。"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.shared.event_bus import EventBus
from src.orchestrators.collaboration.coordinator import CollaborationCoordinator


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
    def __init__(self):
        self.calls = []

    def execute_with(self, text, role, system_prompt="", turn_context=None):
        self.calls.append({"text": text, "role": role,
                           "prompt_has_persona": bool(system_prompt)})
        return {"ok": True, "data": {"reply": "ok", "audio_id": "a1"}}


def test_danmaku_arbitrates_and_executes():
    bus = EventBus()
    pipeline = FakePipeline()
    co = CollaborationCoordinator(bus, pipeline=pipeline, profiles=FakeProfiles(),
                                  trigger_probability=0.0, awareness_enabled=True)
    co.start()
    bus.publish("danmaku:received", content="@Lilith 你同意吗", user_name="观众甲")
    co.flush()   # 同步队列处理
    assert pipeline.calls and pipeline.calls[0]["role"] == "lilith"
    assert pipeline.calls[0]["prompt_has_persona"] is True
    co.stop()


def test_speech_completed_triggers_banter():
    bus = EventBus()
    pipeline = FakePipeline()
    co = CollaborationCoordinator(bus, pipeline=pipeline, profiles=FakeProfiles(),
                                  trigger_probability=1.0, trigger_global_cooldown=0.0,
                                  awareness_enabled=True)
    co.start()
    bus.publish("danmaku:received", content="讲个故事吧", user_name="观众甲")
    co.flush()
    assert len(pipeline.calls) >= 1
    # 模拟 pipeline 发言完成（真实链路中 execute_with 内部发布 speech:completed）
    spoke = pipeline.calls[0]["role"]
    bus.publish("speech:completed", role=spoke, text="回复内容", audio_id="a1")
    co.flush()
    # 触发接话：发言完成后另一方提案（冷却为 0 且概率 1 → 应出现接话调用）
    assert len(pipeline.calls) >= 2
    co.stop()
