"""tool_registry.py — LLM 工具注册表（指挥官层，P1 补迁：旧系统 tool_registry）

LLM 对话内工具调用：system_prompt 注入可用工具清单（prompt 约定格式），
LLM 回复形如 `[[TOOL:工具名:参数]]` 时，管线执行 handler 并把结果回填对话，
再让 LLM 生成最终回复。不依赖引擎原生 tool-calling API，fast/pro 引擎通用。

内置工具：worldbook_lookup（世界书检索）、system_status（系统状态）。

# 模块内容清单（8 项契约）
1. 模块身份标识：commander · tool_registry · LLM 工具注册表
2. 配置契约：无（工具在构造时注册）
3. 输入契约：register(name, description, handler) / list_tools() / execute(name, arg) /
   prompt_block() 生成 system_prompt 工具清单
4. 输出契约：execute 返回工具结果字符串（成功）或错误描述（失败不抛）；list_tools 返回 [(name, desc)]
5. 依赖声明：src.shared.world_book.get_world_book
6. 错误定义：execute 未注册工具 / handler 异常 → 返回 "工具错误: ..." 字符串，不抛
7. 生命周期方法：无（构造即注册内置工具）
8. 领域状态说明：_tools（name → {description, handler}），无持久化
"""
import logging
import re
from typing import Callable, Dict, List, Optional, Tuple

from src.shared.world_book import get_world_book

logger = logging.getLogger(__name__)

# LLM 工具调用标记：[[TOOL:工具名:参数]]
TOOL_CALL_RE = re.compile(r"\[\[TOOL:(\w+):([^\]]*)\]\]")


class ToolRegistry:
    """LLM 工具注册表：注册/列举/执行。"""

    def __init__(self):
        self._tools: Dict[str, Dict] = {}
        self._register_builtin()

    def _register_builtin(self) -> None:
        """内置工具（无外部依赖，跨域只读）。"""
        def _worldbook_lookup(arg: str) -> str:
            try:
                hits = get_world_book().search(arg.strip())[:3]
                if not hits:
                    return "未找到相关世界书条目"
                return "\n".join("- {}：{}".format(e["title"], e["content"][:80])
                                 for e in hits)
            except Exception as e:
                return "世界书查询失败: {}".format(e)

        def _system_status(arg: str) -> str:
            try:
                wb = get_world_book().stats()
                return "世界书条目 {} 条，版本 v{}".format(
                    wb.get("total_entries", 0), wb.get("version", 1))
            except Exception as e:
                return "状态查询失败: {}".format(e)

        self.register("worldbook_lookup", "查询角色世界书设定，参数为关键词",
                      _worldbook_lookup)
        self.register("system_status", "查询系统运行状态（世界书条目数等）",
                      _system_status)

    def register(self, name: str, description: str, handler: Callable[[str], str]) -> None:
        """注册工具：name 唯一，handler 接收参数字符串返回结果字符串。"""
        self._tools[name] = {"description": description, "handler": handler}

    def list_tools(self) -> List[Tuple[str, str]]:
        return [(n, t["description"]) for n, t in self._tools.items()]

    def has(self, name: str) -> bool:
        return name in self._tools

    def prompt_block(self) -> str:
        """生成注入 system_prompt 的工具清单块。"""
        if not self._tools:
            return ""
        lines = ["【可用工具】需要时可输出 [[TOOL:工具名:参数]] 调用工具，"
                 "工具结果会返回给你，再基于结果回复。"]
        for name, desc in self.list_tools():
            lines.append("- {}：{}".format(name, desc))
        return "\n".join(lines)

    def execute(self, name: str, arg: str = "") -> str:
        """执行工具：返回结果字符串；未注册/异常返回错误描述（不抛）。"""
        tool = self._tools.get(name)
        if tool is None:
            return "工具错误: 未注册工具 {}".format(name)
        try:
            result = tool["handler"](arg or "")
            return str(result)
        except Exception as e:
            logger.warning("[ToolRegistry] 工具 %s 执行异常: %s", name, e)
            return "工具错误: {}".format(e)
