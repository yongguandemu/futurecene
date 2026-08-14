"""test_intent_parser.py — 意图解析（规格书 4.4 规则集）"""
from src.commander.intent_parser import IntentParser


def _parse(text, source="danmaku"):
    return IntentParser().parse(text, source=source)


def test_normal_danmaku_routes_to_llm_chat():
    cmd = _parse("今天天气怎么样")
    assert cmd.capability == "llm:chat"
    assert cmd.payload == {"text": "今天天气怎么样"}
    assert cmd.source == "danmaku"


def test_switch_role_command():
    cmd = _parse("!切换 lilith")
    assert cmd.capability == "session:switch"
    assert cmd.payload == {"role": "lilith"}


def test_switch_role_default_yuki():
    cmd = _parse("!切换 yuki")
    assert cmd.capability == "session:switch"
    assert cmd.payload == {"role": "yuki"}


def test_point_song_command():
    cmd = _parse("!点歌 晴天")
    assert cmd.capability == "llm:chat"  # P2 后改 music:request
    assert cmd.payload["intent"] == "request_song"


def test_status_command():
    cmd = _parse("!状态")
    assert cmd.capability == "system:status"


def test_unknown_bang_command():
    cmd = _parse("!随便什么")
    assert cmd.capability == "system:command"


def test_empty_text():
    cmd = _parse("   ")
    assert cmd.capability == "llm:chat"


def test_command_fields():
    cmd = _parse("你好", source="voice")
    assert cmd.session_id == "default"
    assert cmd.raw == "你好"
