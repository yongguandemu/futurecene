"""test_danmaku_pipeline.py — 弹幕→对话管线（规格书 9.2，指挥官层订阅）

ADR-007：管线不再含输入/输出安全过滤环节（信任厂商安全系统）。
"""
from types import SimpleNamespace

from src.commander.danmaku_pipeline import DanmakuPipeline, GIFT_THANK_INTERVAL
from src.commander.session_context import SessionContext
from src.shared.event_bus import EventBus
from src.shared.events import DANMAKU_RECEIVED, FRONTEND_SUBTITLE_UPDATE, GIFT_RECEIVED


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


def _make_pipeline(llm=None, tts=None, session=None, memory=None,
                   profile_loader=None, tool_registry=None):
    bus = EventBus()
    bus.reset()
    pipe = DanmakuPipeline(event_bus=bus, llm_orchestrator=llm, tts_orchestrator=tts,
                           memory_orchestrator=memory,
                           session=session, profile_loader=profile_loader,
                           tool_registry=tool_registry)
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


def test_active_speaker_publishes_subtitle_and_tts():
    """dialogue:active → 主动发言（字幕 + TTS + 记忆 + 发言完成事件）。"""
    from src.shared.events import ACTIVE_DIALOGUE, SPEECH_COMPLETED
    bus = EventBus()
    bus.reset()
    llm = FakeLLM(reply="主动说话内容")
    tts = FakeTTS()
    memory = FakeMemory()
    pipe = DanmakuPipeline(event_bus=bus, llm_orchestrator=llm,
                           tts_orchestrator=tts, memory_orchestrator=memory)
    seen = {}
    bus.subscribe(FRONTEND_SUBTITLE_UPDATE, lambda event, **kw: seen.update(kw))
    completed = []
    bus.subscribe(SPEECH_COMPLETED, lambda event, **kw: completed.append(kw))
    pipe.start()
    bus.publish(ACTIVE_DIALOGUE, text="今天播点什么好呢",
                mood="default", role="yuki", timestamp=0.0)
    assert seen.get("text") == "今天播点什么好呢"
    assert tts.calls and tts.calls[0]["capability"] in ("tts:synthesize", "tts:stream_synthesize")
    # 记忆存储已调用（主动对话只存 assistant 消息）
    store_calls = [c for c in memory.calls if c["capability"] == "memory:store"]
    assert len(store_calls) == 1
    assert store_calls[0]["payload"]["role"] == "assistant"
    # 发言完成事件已发布
    assert completed and completed[0]["text"] == "今天播点什么好呢"
    assert completed[0]["role"] == "yuki"


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

    bus = EventBus()
    llm = FakeLLM()
    pipe = DanmakuPipeline(bus, llm_orchestrator=llm,
                           tts_orchestrator=FakeTTS())
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
    """Task 15 评审补覆盖：不传显式 system_prompt 时，沿用 profile_loader 注入的画像 prompt + 世界书核心设定。"""
    import asyncio
    llm = FakeLLM(reply="你好呀")
    tts = FakeTTS()
    profile_loader = FakeProfileLoader(prompts={"lilith": "你是Lilith（画像注入）"})
    pipe, bus = _make_pipeline(llm=llm, tts=tts, profile_loader=profile_loader)
    result = asyncio.run(pipe.execute_with("你好", role="lilith"))
    assert result["ok"] is True
    assert llm.calls and llm.calls[0]["payload"]["role"] == "lilith"
    sp = llm.calls[0]["payload"]["system_prompt"]
    assert "你是Lilith（画像注入）" in sp
    assert "【世界设定】" in sp and "莉莉丝" in sp  # 世界书核心条目已注入
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


# =====================================================================
# 礼物 → LLM 对话（信息输入同管线，含节流）
# =====================================================================

def _publish_gift(bus, content="小星星 x1", user_name="土豪观众"):
    bus.publish(GIFT_RECEIVED, content=content, user_name=user_name,
                event_type="gift",
                extra={"num": 1, "price": 100, "gift_name": "小星星"})


def test_gift_event_triggers_llm_thanks():
    """礼物事件：构造「【礼物】XX 送出了 Y」输入走 LLM 对话链路，感谢文本进 TTS。"""
    llm = FakeLLM(reply="谢谢老板～")
    tts = FakeTTS()
    pipe, bus = _make_pipeline(llm=llm, tts=tts)
    _publish_gift(bus)
    assert llm.calls
    payload = llm.calls[0]["payload"]
    assert "【礼物】" in payload["text"] and "土豪观众" in payload["text"]
    assert "小星星 x1" in payload["text"]
    assert tts.calls  # 感谢回复走了 TTS 合成


def test_gift_thanks_throttled():
    """节流：GIFT_THANK_INTERVAL 秒内多条礼物合并为一次 LLM 感谢。"""
    llm = FakeLLM(reply="谢谢")
    tts = FakeTTS()
    pipe, bus = _make_pipeline(llm=llm, tts=tts)
    _publish_gift(bus, user_name="观众A")
    _publish_gift(bus, user_name="观众B")  # 紧随其后 → 节流跳过
    assert len(llm.calls) == 1
    assert "观众A" in llm.calls[0]["payload"]["text"]


def test_gift_ignored_without_llm():
    """LLM 未注入：礼物事件跳过，不抛异常。"""
    pipe, bus = _make_pipeline(llm=None, tts=None)
    _publish_gift(bus)
    assert pipe._last_gift_at == 0.0  # 未触发对话，节流时间戳未更新


def test_gift_ignored_without_user():
    """缺 user_name / content：不进入对话链路。"""
    llm = FakeLLM(reply="谢谢")
    pipe, bus = _make_pipeline(llm=llm)
    bus.publish(GIFT_RECEIVED, content="", user_name="")
    assert not llm.calls


# =====================================================================
# LLM 工具调用（P1：[[TOOL:name:arg]] 执行循环）
# =====================================================================

class FakeToolLLM:
    """多轮回复：第 1 轮返回工具调用标记，之后返回最终回复。"""

    def __init__(self, tool_reply='[[TOOL:worldbook_lookup:Yuki 的身份]]',
                 final_reply="她是 AI 实习生～"):
        self.tool_reply = tool_reply
        self.final_reply = final_reply
        self.calls = []

    async def handle(self, command):
        self.calls.append(command)
        reply = self.tool_reply if len(self.calls) == 1 else self.final_reply
        return {"ok": True, "data": {"reply": reply, "usage": {}}, "error": None}


def test_tool_call_loop_executes_and_returns_final():
    """工具调用循环：[[TOOL:...]] → 执行 → 结果回填 history → 最终回复。"""
    import asyncio
    from src.commander.tool_registry import ToolRegistry
    llm = FakeToolLLM()
    tts = FakeTTS()
    pipe, bus = _make_pipeline(llm=llm, tts=tts, tool_registry=ToolRegistry())
    result = asyncio.run(pipe.execute_with("Yuki 是什么设定？", role="yuki"))
    assert result["ok"] is True
    assert result["data"]["reply"] == "她是 AI 实习生～"
    assert len(llm.calls) == 2  # 工具调用轮 + 最终轮
    # 第二轮 history 包含工具结果回填
    history = llm.calls[1]["payload"]["history"]
    assert any("工具 worldbook_lookup 结果" in str(h) for h in history)
    assert tts.calls  # 最终回复走了 TTS


def test_tool_loop_plain_reply_single_call():
    """LLM 直接返回普通文本（无工具标记）→ 只调一次。"""
    import asyncio
    from src.commander.tool_registry import ToolRegistry
    llm = FakeLLM(reply="直接回答")
    pipe, bus = _make_pipeline(llm=llm, tool_registry=ToolRegistry())
    result = asyncio.run(pipe.execute_with("你好", role="yuki"))
    assert result["data"]["reply"] == "直接回答"
    assert len(llm.calls) == 1


def test_tool_loop_unregistered_tool_passthrough():
    """未注册工具：不拦截，原样返回（不进入循环）。"""
    import asyncio
    from src.commander.tool_registry import ToolRegistry
    llm = FakeToolLLM(tool_reply="[[TOOL:no_such_tool:x]]")
    pipe, bus = _make_pipeline(llm=llm, tool_registry=ToolRegistry())
    result = asyncio.run(pipe.execute_with("hi", role="yuki"))
    assert result["data"]["reply"] == "[[TOOL:no_such_tool:x]]"
    assert len(llm.calls) == 1
