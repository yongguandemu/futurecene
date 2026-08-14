"""test_danmaku_pipeline.py — 弹幕→对话管线（规格书 9.2，指挥官层订阅）"""
from src.commander.danmaku_pipeline import DanmakuPipeline
from src.commander.session_context import SessionContext
from src.shared.event_bus import EventBus
from src.shared.events import DANMAKU_RECEIVED, FRONTEND_SUBTITLE_UPDATE


class FakeLLM:
    def __init__(self, reply="你好呀", fail=False):
        self.reply = reply
        self.fail = fail
        self.calls = []

    async def handle(self, command):
        self.calls.append(command)
        if self.fail:
            return {"ok": False, "data": {}, "error": "llm down"}
        return {"ok": True, "data": {"reply": self.reply, "usage": {}}, "error": None}


class FakeTTS:
    def __init__(self):
        self.calls = []

    async def handle(self, command):
        self.calls.append(command)
        return {"ok": True, "data": {"audio_id": "audio-1", "duration_ms": 100}, "error": None}


def _make_pipeline(llm=None, tts=None, session=None):
    bus = EventBus()
    bus.reset()
    pipe = DanmakuPipeline(event_bus=bus, llm_orchestrator=llm, tts_orchestrator=tts,
                           session=session)
    pipe.start()
    return pipe, bus


def _publish_danmaku(bus, content="你好", user_name="观众"):
    bus.publish(DANMAKU_RECEIVED, event_type="danmaku", content=content,
                user_name=user_name, user_id="1", extra={}, timestamp=0.0)


def test_danmaku_triggers_llm_and_subtitle():
    llm = FakeLLM(reply="你好呀")
    pipe, bus = _make_pipeline(llm=llm)
    seen = {}
    bus.subscribe(FRONTEND_SUBTITLE_UPDATE, lambda event, **kw: seen.update(kw))
    _publish_danmaku(bus)
    assert llm.calls and llm.calls[0]["capability"] == "llm:chat"
    seen.pop("seq", None)  # seq 为事件元数据，不属于业务载荷
    assert seen == {"text": "你好呀", "role": "yuki", "user_name": "观众"}


def test_role_reads_from_session_context():
    """角色实时取自 SessionContext：切换后字幕/LLM/TTS 均使用当前角色。"""
    llm = FakeLLM(reply="你好呀")
    tts = FakeTTS()
    session = SessionContext(session_id="default", role="lilith")
    pipe, bus = _make_pipeline(llm=llm, tts=tts, session=session)
    seen = {}
    bus.subscribe(FRONTEND_SUBTITLE_UPDATE, lambda event, **kw: seen.update(kw))
    _publish_danmaku(bus)
    # 字幕事件 role
    seen.pop("seq", None)
    assert seen["role"] == "lilith"
    # LLM payload role
    assert llm.calls[0]["payload"]["role"] == "lilith"
    # TTS payload role
    assert tts.calls[0]["payload"]["role"] == "lilith"
    # 切换角色后实时生效（不缓存）
    session.switch_role("yuki")
    _publish_danmaku(bus)
    assert llm.calls[1]["payload"]["role"] == "yuki"


def test_system_command_skipped():
    llm = FakeLLM()
    pipe, bus = _make_pipeline(llm=llm)
    _publish_danmaku(bus, content="!点歌 晴天")
    assert llm.calls == []  # 系统命令不触发 LLM


def test_empty_content_skipped():
    llm = FakeLLM()
    pipe, bus = _make_pipeline(llm=llm)
    _publish_danmaku(bus, content="   ")
    assert llm.calls == []


def test_no_llm_skips_reply():
    pipe, bus = _make_pipeline(llm=None)
    seen = {}
    bus.subscribe(FRONTEND_SUBTITLE_UPDATE, lambda event, **kw: seen.update(kw))
    _publish_danmaku(bus)
    assert seen == {}


def test_llm_failure_no_subtitle():
    llm = FakeLLM(fail=True)
    pipe, bus = _make_pipeline(llm=llm)
    seen = {}
    bus.subscribe(FRONTEND_SUBTITLE_UPDATE, lambda event, **kw: seen.update(kw))
    _publish_danmaku(bus)
    assert seen == {}


def test_tts_invoked_when_injected():
    llm = FakeLLM(reply="合成我")
    tts = FakeTTS()
    pipe, bus = _make_pipeline(llm=llm, tts=tts)
    _publish_danmaku(bus)
    assert tts.calls and tts.calls[0]["capability"] == "tts:synthesize"
    assert tts.calls[0]["payload"]["text"] == "合成我"


def test_stop_unsubscribes():
    llm = FakeLLM()
    pipe, bus = _make_pipeline(llm=llm)
    pipe.stop()
    _publish_danmaku(bus)
    assert llm.calls == []
