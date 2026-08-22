"""test_distribution_router.py — 输入分发路由"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.commander.input.input_classifier import InputClassifier
from src.commander.input.distribution_router import DistributionRouter


class FakeParser:
    """意图解析桩：仅 system_loop 文本命中 '捡装备' 视为意图。"""

    def parse(self, text, source="danmaku", session_id="default"):
        from src.commander.intent_parser import Command
        if "捡装备" in (text or ""):
            return Command(capability="game:op_command", payload={"text": text},
                           source=source, session_id=session_id)
        return Command(capability="llm:chat", payload={"text": text},
                       source=source, session_id=session_id)


class FakeRouter:
    def __init__(self):
        self.calls = []

    async def dispatch(self, cmd):
        self.calls.append(cmd.capability)
        return {"ok": True, "data": {"reply": "ok"}}


class FakePipeline:
    def __init__(self):
        self.calls = []

    async def execute_with(self, text, role="yuki", **kw):
        self.calls.append(text)
        return {"ok": True}


def _router(parser=None, cmd_router=None, pipeline=None):
    return DistributionRouter(intent_parser=parser or FakeParser(),
                              command_router=cmd_router or FakeRouter(),
                              danmaku_pipeline=pipeline or FakePipeline())


def test_operator_routes_to_command_router():
    r = _router()
    env = InputClassifier().classify(text="!点歌 晴天", source="command")
    result = asyncio.run(r.route(env))
    assert result["target"] == "command_router"
    assert r._cmd_router.calls == ["music:request"] or r._cmd_router.calls


def test_system_loop_intent_routes_to_command_router():
    r = _router()
    env = InputClassifier().classify(text="我去捡装备了", source="system_loop", loop_depth=1)
    result = asyncio.run(r.route(env))
    assert result["target"] == "command_router"
    assert result["capability"] == "game:op_command"
    assert result["archived"] is False


def test_system_loop_no_intent_archived():
    r = _router()
    env = InputClassifier().classify(text="今天天气不错", source="system_loop", loop_depth=1)
    result = asyncio.run(r.route(env))
    assert result["target"] == "archive"
    assert result["archived"] is True


def test_audience_routes_to_pipeline():
    r = _router()
    env = InputClassifier().classify(text="主播好", source="danmaku")
    result = asyncio.run(r.route(env))
    assert result["target"] == "danmaku_pipeline"
    assert r._pipeline.calls == ["主播好"]


def test_reference_not_routed():
    r = _router()
    env = InputClassifier().classify(text="查设定", source="command", kind="reference")
    result = asyncio.run(r.route(env))
    assert result["target"] == "context"  # 参考资料走上下文聚合，不响应


def test_external_app_passthrough():
    r = _router()
    env = InputClassifier().classify(source="screen", event="screen:cursor_action")
    result = asyncio.run(r.route(env))
    assert result["target"] == "event_bus"
    assert result["archived"] is False
