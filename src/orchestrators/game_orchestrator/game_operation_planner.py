"""game_operation_planner.py — 游戏操作规划器（P3 通用游戏操作）

把自然语言/结构化指令转换为可执行操作计划。双路径：
- 模板匹配（确定性，快路径）：常见游戏操作关键词 → 预定义计划
- LLM 生成（慢路径）：模板未命中时经注入的 chat_fn 生成 JSON 计划
- 计划校验：合法动作白名单 + 参数完整性

操作类型（与 screen:execute_plan 对齐 + 游戏扩展）：
click / move / keypress / hold / release / type / wait

# 模块内容清单 — game_operation_planner

## 1. 模块身份标识
- 所属调度官：game
- 能力名：game:op_plan（承载实现）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| 无独立配置 | - | - | - | 模板表 + 白名单为静态常量 |

## 3. 输入契约
- 输入格式：`GameOperationPlanner(chat_fn=None)`；`generate_plan(command, screen_description=None) -> List[dict]`
- chat_fn：`fn(prompt) -> str`，返回 LLM 文本（未注入时跳过 LLM 路径）

## 4. 输出契约
- 成功：返回操作计划列表（空列表表示无法理解）
- 失败：LLM 异常 → 返回空列表并记录日志
- 事件：无

## 5. 依赖声明
- 外部服务：无（LLM 经注入 chat_fn，可选）
- 内部模块：json、re、logging
- 预先配置：GameOperationLoop 构造时注入 chat_fn（可选）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| LLM 返回非法 JSON | 模型输出非 JSON | 清理 markdown 后重试解析，失败返回空列表 |
| 计划含未知动作 | 模型幻觉 | validate_plan 拦截并返回错误信息 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | 否 | 纯函数式模块（generate_plan/validate_plan） |

## 8. 领域状态说明
- 状态项：无（无状态；模板表为静态常量）
- 持久化：无
- 恢复：无
"""
import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 合法动作白名单
VALID_ACTIONS = {"click", "move", "keypress", "hold", "release", "type", "wait"}

# 预定义操作模板（确定性快路径）
ACTION_TEMPLATES = {
    "前进": [{"action": "hold", "params": {"key": "W"}}],
    "向前": [{"action": "hold", "params": {"key": "W"}}],
    "后退": [{"action": "hold", "params": {"key": "S"}}],
    "向后": [{"action": "hold", "params": {"key": "S"}}],
    "向左": [{"action": "hold", "params": {"key": "A"}}],
    "向右": [{"action": "hold", "params": {"key": "D"}}],
    "跳跃": [{"action": "keypress", "params": {"key": "SPACE"}}],
    "攻击": [{"action": "click", "params": {"button": "left"}}],
    "互动": [{"action": "keypress", "params": {"key": "E"}}],
    "打开背包": [{"action": "keypress", "params": {"key": "B"}}],
    "打开菜单": [{"action": "keypress", "params": {"key": "ESC"}}],
    "确认": [{"action": "keypress", "params": {"key": "ENTER"}}],
    "取消": [{"action": "keypress", "params": {"key": "ESC"}}],
    "截图": [{"action": "capture", "params": {}}],
    "等待": [{"action": "wait", "params": {"seconds": 1.0}}],
}

# LLM 系统提示词
_SYSTEM_PROMPT = """你是一个游戏操作规划助手。把用户的自然语言指令转换为可执行的操作序列。
操作类型（JSON 数组）：
- click: 鼠标点击 (button='left'/'right', x, y 可选)
- move: 移动鼠标 (x, y)
- keypress: 按键 (key，如 W/A/S/D/SPACE/ENTER/ESC)
- hold: 按住按键 (key)
- release: 松开按键 (key)
- type: 输入文字 (text)
- wait: 等待 (seconds)
只返回 JSON 数组，不要返回其他内容。示例：
[{"action": "keypress", "params": {"key": "W"}}]
如果无法理解指令，返回 []。"""


class GameOperationPlanner:
    """游戏操作规划器：模板 + LLM 双路径生成操作计划。"""

    def __init__(self, chat_fn: Optional[Callable[[str], str]] = None):
        self._chat_fn = chat_fn

    def generate_plan(self, command: str,
                      screen_description: Optional[str] = None) -> List[Dict[str, Any]]:
        """生成操作计划。command: 自然语言/结构化指令。"""
        if not command or not command.strip():
            return []

        # 1. 模板匹配（快速确定性）
        plan = self._match_template(command)
        if plan:
            return plan

        # 2. LLM 生成（较慢）
        plan = self._generate_with_llm(command, screen_description)
        if plan:
            return plan

        logger.warning("[GameOperationPlanner] 无法理解指令: %s", command)
        return []

    def _match_template(self, command: str) -> Optional[List[Dict[str, Any]]]:
        cmd = command.strip().lower()
        for keyword, plan in ACTION_TEMPLATES.items():
            if keyword in cmd:
                logger.debug("[GameOperationPlanner] 模板命中: %s", keyword)
                return [dict(a) for a in plan]
        return None

    def _generate_with_llm(self, command: str,
                           screen_description: Optional[str]) -> Optional[List[Dict[str, Any]]]:
        if self._chat_fn is None:
            return None
        try:
            prompt = _SYSTEM_PROMPT + "\n\n用户指令：{}\n".format(command)
            if screen_description:
                prompt += "当前画面：{}\n".format(screen_description)
            content = self._chat_fn(prompt) or ""
            content = re.sub(r"```json\s*", "", content)
            content = re.sub(r"```\s*", "", content)
            content = content.strip()
            if not content:
                return None
            plan = json.loads(content)
            if not isinstance(plan, list):
                return None
            ok, _ = self.validate_plan(plan)
            return plan if ok else None
        except Exception as e:
            logger.error("[GameOperationPlanner] LLM 规划失败: %s", e)
            return None

    @staticmethod
    def validate_plan(plan: List[Dict[str, Any]]) -> Tuple[bool, str]:
        """校验计划合法性。返回 (是否合法, 错误信息)。"""
        if not plan:
            return False, "计划为空"
        for i, step in enumerate(plan):
            if not isinstance(step, dict):
                return False, f"操作 {i + 1} 格式错误"
            action = step.get("action")
            if action not in VALID_ACTIONS:
                return False, f"操作 {i + 1} 未知动作: {action}"
            if action in ("click", "move") and \
                    step.get("params", {}).get("x") is None:
                return False, f"操作 {i + 1} 缺少坐标 x"
            if action in ("keypress", "hold", "release") and \
                    not step.get("params", {}).get("key"):
                return False, f"操作 {i + 1} 缺少按键 key"
        return True, "计划合法"
