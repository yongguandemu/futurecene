"""test_danmaku_pipeline.py — 弹幕→对话管线（规格书 9.2，指挥官层订阅）"""
from types import SimpleNamespace

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


class FakeSafety:
    def __init__(self, verdict="allow"):
        self.verdict = verdict
        self.calls = []

    async def handle(self, command):
        self.calls.append(command)
        return {"ok": True, "data": {"verdict": self.verdict, "reason": ""}, "error": None}


class FakeMemory:
    """记录 memory:retrieve/memory:store 的全部调用 payload。"""

    def __init__(self):
        self.calls = []

    async def handle(self, command):
        self.calls.append(command)
        if command["capability"] == "memory:retrieve":
            return {"ok": True, "data": {"memories": []}, "error": None}
        return {"ok": True, "data": {}, "error": None}


class FakeProfileLoader:
    """角色画像注入：load(role) 返回带 system_prompt 的画像（角色缺失返回 None）。"""

    def __init__(self, prompts=None):
        self._prompts = prompts or {}

    def load(self, role):
        sp = self._prompts.get(role, "")
        return SimpleNamespace(system_prompt=sp) if sp else None


def _make_pipeline(llm=None, tts=None, session=None, safety=None, memory=None,
                   profile_loader=None):
    bus = EventBus()
    bus.reset()
    pipe = DanmakuPipeline(event_bus=bus, llm_orchestrator=llm, tts_orchestrator=tts,
                           safety_orchestrator=safety, memory_orchestrator=memory,
                           session=session, profile_loader=profile_loader)
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


def test_execute_with_uses_role_and_publishes_completed():
    """Task 15：execute_with 参数化入口按指定角色执行，字幕/LLM 带 role，
    显式 system_prompt 优先（不叠加 profile_loader 注入），链路末尾发布 speech:completed(role)。"""
    import asyncio

    class FakeLLM:
        def __init__(self):
            self.calls = []

        async def handle(self, cmd):
            self.calls.append(cmd)
            return {"ok": True, "data": {"reply": "回复内容"}}

    class FakeTTS:
        async def handle(self, cmd):
            return {"ok": True, "data": {"audio_id": "a1"}}

    class FakeSafety:
        async def handle(self, cmd):
            return {"ok": True, "data": {"verdict": "allow"}}

    bus = EventBus()
    llm = FakeLLM()
    pipe = DanmakuPipeline(bus, llm_orchestrator=llm,
                           tts_orchestrator=FakeTTS(),
                           safety_orchestrator=FakeSafety())
    events = []
    # EventBus 订阅回调签名：event 名以 event= 关键字传入，归一化为 type 便于断言
    bus.subscribe("speech:completed",
                  lambda event, **kw: events.append({"type": event, **kw}))
    bus.subscribe("frontend:subtitle_update",
                  lambda event, **kw: events.append({"type": event, **kw}))
    asyncio.run(pipe.execute_with("你好", role="lilith",
                                  system_prompt="你是Lilith", turn_context=[]))
    assert events and events[0]["type"] == "frontend:subtitle_update"
    assert events[0]["role"] == "lilith"
    assert events[-1]["type"] == "speech:completed"
    assert events[-1]["role"] == "lilith"
    # 显式 system_prompt 优先：LLM payload 用传入值（不叠加 profile_loader 注入）
    assert llm.calls and llm.calls[0]["payload"]["role"] == "lilith"
    assert llm.calls[0]["payload"]["system_prompt"] == "你是Lilith"


def test_execute_with_no_llm_returns_llm_not_injected():
    """Task 15 评审修复：execute_with 入口守卫——LLM 未注入直接返回
    {"ok": False, "error": "llm-not-injected"}，不进入 _process 误记 llm_empty_reply。"""
    import asyncio
    pipe, bus = _make_pipeline(llm=None)
    result = asyncio.run(pipe.execute_with("你好", role="lilith"))
    assert result == {"ok": False, "error": "llm-not-injected"}


def test_execute_with_fallback_system_prompt_from_profile_loader():
    """Task 15 评审补覆盖：不传显式 system_prompt 时，沿用 profile_loader 注入的画像 prompt。"""
    import asyncio
    llm = FakeLLM(reply="你好呀")
    tts = FakeTTS()
    profile_loader = FakeProfileLoader(prompts={"lilith": "你是Lilith（画像注入）"})
    pipe, bus = _make_pipeline(llm=llm, tts=tts, profile_loader=profile_loader)
    result = asyncio.run(pipe.execute_with("你好", role="lilith"))
    assert result["ok"] is True
    assert llm.calls and llm.calls[0]["payload"]["role"] == "lilith"
    assert llm.calls[0]["payload"]["system_prompt"] == "你是Lilith（画像注入）"
    assert tts.calls[0]["payload"]["role"] == "lilith"


def test_execute_with_turn_context_forwarded_to_llm():
    """Task 15 评审补覆盖：非空 turn_context 原样进 LLM payload。"""
    import asyncio
    llm = FakeLLM(reply="你好呀")
    pipe, bus = _make_pipeline(llm=llm)
    turn_context = [{"role": "user", "content": "Lilith 刚说过：今天天气不错"}]
    asyncio.run(pipe.execute_with("你好", role="lilith",
                                  system_prompt="你是Lilith",
                                  turn_context=turn_context))
    assert llm.calls and llm.calls[0]["payload"]["turn_context"] == turn_context


def test_execute_with_returns_ok_and_completed_audio_id():
    """Task 15 评审补覆盖：execute_with 返回值 {"ok": True, data:{reply, audio_id}}，
    speech:completed 携带同一 audio_id/role/text。"""
    import asyncio
    llm = FakeLLM(reply="合成我")
    tts = FakeTTS()
    pipe, bus = _make_pipeline(llm=llm, tts=tts)
    events = []
    bus.subscribe("speech:completed", lambda event, **kw: events.append(kw))
    result = asyncio.run(pipe.execute_with("你好", role="lilith",
                                           system_prompt="你是Lilith"))
    assert result["ok"] is True
    assert result["data"]["reply"] == "合成我"
    assert result["data"]["audio_id"] == "audio-1"
    assert events and events[-1]["audio_id"] == "audio-1"
    assert events[-1]["role"] == "lilith"
    assert events[-1]["text"] == "合成我"


def test_execute_with_memory_store_carries_character_id():
    """Task 15 评审补覆盖：记忆按发言角色分桶——memory:store 的 user/assistant
    两条 payload 均携带 character_id=role，消息角色 role 字段不受影响。"""
    import asyncio
    llm = FakeLLM(reply="你好呀")
    memory = FakeMemory()
    pipe, bus = _make_pipeline(llm=llm, memory=memory)
    result = asyncio.run(pipe.execute_with("你好", role="lilith"))
    assert result["ok"] is True
    store_calls = [c for c in memory.calls if c["capability"] == "memory:store"]
    assert len(store_calls) == 2
    assert store_calls[0]["payload"]["character_id"] == "lilith"
    assert store_calls[0]["payload"]["role"] == "user"
    assert store_calls[1]["payload"]["character_id"] == "lilith"
    assert store_calls[1]["payload"]["role"] == "assistant"
    # 记忆检索同样按角色分桶
    retrieve_calls = [c for c in memory.calls if c["capability"] == "memory:retrieve"]
    assert retrieve_calls and retrieve_calls[0]["payload"]["character_id"] == "lilith"
