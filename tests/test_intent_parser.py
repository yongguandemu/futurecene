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


def test_obs_sources_command():
    for text in ("有哪些浏览器源", "OBS源清单", "直播源地址", "浏览器源"):
        cmd = _parse(text)
        assert cmd.capability == "obs:sources", text


def test_obs_open_command():
    cmd = _parse("打开字幕源")
    assert cmd.capability == "obs:open"
    assert cmd.payload["key"] == "字幕"
    cmd = _parse("打开弹幕显示源")
    assert cmd.capability == "obs:open"
    assert cmd.payload["key"] == "弹幕显示"
    cmd = _parse("打开Live2D源")
    assert cmd.capability == "obs:open"
    assert cmd.payload["key"] == "live2d"


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


def test_live2d_load_intent():
    cmd = _parse("加载小恶魔模型")
    assert cmd.capability == "live2d:load"
    assert cmd.payload.get("model_name") == "小恶魔"


def test_live2d_load_yuki_intent():
    cmd = _parse("加载 Hiyori 模型")
    assert cmd.capability == "live2d:load"
    assert cmd.payload.get("model_name") == "Hiyori"


def test_live2d_expression_intent():
    cmd = _parse("做个开心的表情")
    assert cmd.capability == "live2d:expression"
    assert cmd.payload.get("expression") == "开心"


def test_live2d_motion_intent():
    cmd = _parse("挥挥手")
    assert cmd.capability == "live2d:motion"
    assert cmd.payload.get("motion") == "wave"


def test_live2d_prepare_intent():
    cmd = _parse("准备直播界面")
    assert cmd.capability == "live2d:prepare"
