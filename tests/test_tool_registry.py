"""test_tool_registry.py — LLM 工具注册表单测（P1 补迁）"""
from src.commander.tool_registry import TOOL_CALL_RE, ToolRegistry


def _make_registry():
    reg = ToolRegistry()
    reg.register("echo", "回显参数", lambda arg: "echo:" + arg)
    return reg


def test_builtin_tools_present():
    """内置工具：worldbook_lookup / system_status。"""
    reg = ToolRegistry()
    names = {n for n, _ in reg.list_tools()}
    assert {"worldbook_lookup", "system_status"} <= names


def test_register_and_execute():
    """注册/执行：handler 收到参数，返回结果字符串。"""
    reg = _make_registry()
    assert reg.has("echo") is True
    assert reg.execute("echo", "你好") == "echo:你好"


def test_execute_unknown_tool_returns_error():
    """未注册工具：返回错误描述，不抛异常。"""
    reg = _make_registry()
    result = reg.execute("nope", "x")
    assert "未注册工具" in result


def test_execute_handler_exception_caught():
    """handler 抛异常：返回错误描述，不抛。"""
    reg = _make_registry()
    reg.register("boom", "抛异常", lambda arg: (_ for _ in ()).throw(RuntimeError("x")))
    result = reg.execute("boom")
    assert "工具错误" in result


def test_prompt_block_lists_tools():
    """prompt_block 生成工具清单块（含工具名与描述）。"""
    reg = _make_registry()
    block = reg.prompt_block()
    assert "【可用工具】" in block and "TOOL:" in block
    assert "echo" in block


def test_worldbook_lookup_returns_entries():
    """worldbook_lookup：真实世界书检索命中核心设定。"""
    reg = ToolRegistry()
    result = reg.execute("worldbook_lookup", "Yuki 的身份")
    assert "AI 实习生" in result


def test_tool_call_regex():
    """[[TOOL:name:arg]] 格式解析。"""
    m = TOOL_CALL_RE.search("我先查一下 [[TOOL:worldbook_lookup:架构师]]")
    assert m is not None
    assert m.group(1) == "worldbook_lookup"
    assert m.group(2) == "架构师"
