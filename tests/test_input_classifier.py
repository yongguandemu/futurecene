"""test_input_classifier.py — 输入分类（五类 + 优先级 + 身份/深度标记）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.commander.input.input_classifier import (
    InputClassifier, InputEnvelope, InputType,
)


def _c(text="", source="", event="", **kw):
    return InputClassifier().classify(text=text, source=source, event=event, **kw)


def test_operator_command_source():
    e = _c(text="你好", source="command")
    assert e.input_type == InputType.OPERATOR
    assert e.priority == 0
    assert e.operator_id == "user"


def test_operator_bang_prefix():
    e = _c(text="!点歌 晴天", source="danmaku")
    assert e.input_type == InputType.OPERATOR
    assert e.operator_id == "user"


def test_audience_danmaku():
    e = _c(text="主播好", source="danmaku")
    assert e.input_type == InputType.AUDIENCE
    assert e.priority == 1


def test_audience_gift_event():
    e = _c(source="gift", event="gift:received")
    assert e.input_type == InputType.AUDIENCE


def test_external_app_screen():
    e = _c(source="screen", event="screen:cursor_action")
    assert e.input_type == InputType.EXTERNAL_APP
    assert e.priority == 2


def test_system_loop_source():
    e = _c(source="system_loop", loop_depth=1)
    assert e.input_type == InputType.SYSTEM_LOOP
    assert e.priority == 3
    assert e.loop_depth == 1


def test_reference_worldbook():
    e = _c(text="查询 Yuki 的设定", source="command", kind="reference")
    assert e.input_type == InputType.REFERENCE
    assert e.priority == -1  # 不排队


def test_fallback_audience():
    e = _c(text="未知来源内容", source="unknown_thing")
    assert e.input_type == InputType.AUDIENCE


def test_envelope_is_dataclass():
    e = _c(text="hi", source="command")
    assert isinstance(e, InputEnvelope)
    assert e.source == "command"
