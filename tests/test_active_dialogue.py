"""test_active_dialogue.py — 主动对话引擎测试（role_provider + tick 链路）"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.llm_orchestrator.active_dialogue import ActiveDialogue


def test_set_role_provider_returns_role():
    """set_role_provider 注入的函数被 _timer_loop 用于获取当前角色。"""
    ad = ActiveDialogue(config={"enabled": False, "min_cooldown": 0,
                                "max_silence": 0, "trigger_probability": 1.0})
    ad.set_role_provider(lambda: "lilith")
    assert ad._role_provider() == "lilith"


def test_tick_with_role_uses_role_generator():
    """tick(role) 非空时优先调用 role_generator，传入正确 role。"""
    ad = ActiveDialogue(config={"enabled": False, "min_cooldown": 0,
                                "max_silence": 0, "trigger_probability": 1.0})
    captured = {}

    def role_gen(role):
        captured["role"] = role
        return {"text": "角色化话题", "mood": "happy"}

    ad.set_role_generator(role_gen)
    result = ad.tick(role="yuki")
    assert captured["role"] == "yuki"
    assert result == {"text": "角色化话题", "mood": "happy"}


def test_tick_without_role_falls_back_to_generator():
    """tick() 不带 role 时走 generator（单角色兼容）。"""
    ad = ActiveDialogue(config={"enabled": False, "min_cooldown": 0,
                                "max_silence": 0, "trigger_probability": 1.0})
    called = {"gen": False}

    def gen():
        called["gen"] = True
        return {"text": "通用话题", "mood": "default"}

    ad.set_generator(gen)
    result = ad.tick()
    assert called["gen"] is True
    assert result == {"text": "通用话题", "mood": "default"}


def test_tick_role_generator_failure_falls_back_to_generator():
    """role_generator 异常时回退到 generator。"""
    ad = ActiveDialogue(config={"enabled": False, "min_cooldown": 0,
                                "max_silence": 0, "trigger_probability": 1.0})

    def bad_role_gen(role):
        raise RuntimeError("LLM 调用失败")

    def gen():
        return {"text": "兜底话题", "mood": "calm"}

    ad.set_role_generator(bad_role_gen)
    ad.set_generator(gen)
    result = ad.tick(role="yuki")
    assert result == {"text": "兜底话题", "mood": "calm"}


def test_tick_publishes_event_with_role():
    """tick(role) 发布的 ACTIVE_DIALOGUE 事件携带 role。"""
    from src.shared.event_bus import EventBus
    from src.shared.events import ACTIVE_DIALOGUE

    bus = EventBus()
    bus.reset()
    ad = ActiveDialogue(event_bus=bus, config={"enabled": False, "min_cooldown": 0,
                                                "max_silence": 0, "trigger_probability": 1.0})
    ad.set_role_generator(lambda role: {"text": "测试", "mood": "default"})
    published = {}
    bus.subscribe(ACTIVE_DIALOGUE, lambda event, **kw: published.update(kw))
    ad.tick(role="lilith")
    assert published.get("role") == "lilith"
    assert published.get("text") == "测试"
