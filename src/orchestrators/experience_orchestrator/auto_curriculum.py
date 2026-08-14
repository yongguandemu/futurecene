"""auto_curriculum.py — 自动课程（游戏经验学习域）

Voyager 式：地面真值状态 + 知识库 rules → 下一个目标提议。规则模板驱动（零额外 LLM）。

# 模块内容清单（8 项契约摘录）
- 所属调度官：experience
- 能力名：experience:curriculum
- 配置契约：check_interval(30) / enabled(True)
- 输入契约：tick(state) -> 目标字符串
- 输出契约：下一个目标（空串 = 无目标）
- 生命周期：无（纯函数 + 轻量类）
"""
import logging

logger = logging.getLogger(__name__)

# 课程规则：condition 函数(状态) → 目标字符串。按顺序匹配，返回第一个命中。
CURRICULUM_RULES = [
    ("need_food", lambda s: (s.get("food") or 20) < 6, "找食物"),
    ("need_wood", lambda s: _count(s, "oak_log") < 4, "gather_wood"),
    ("no_crafting_table",
     lambda s: _count(s, "oak_log") >= 4 and _count(s, "crafting_table") == 0,
     "crafting_table"),
    ("no_wooden_pickaxe",
     lambda s: _count(s, "oak_log") >= 4 and _count(s, "wooden_pickaxe") == 0,
     "wooden_pickaxe"),
    ("no_stone_pickaxe",
     lambda s: _count(s, "cobblestone") >= 3 and _count(s, "stone_pickaxe") == 0,
     "stone_pickaxe"),
]


def _count(state, name):
    return sum(i.get("count", 0) for i in (state.get("inventory") or [])
               if i.get("name") == name)


def propose_goal(state: dict) -> str:
    """返回下一个目标（空串 = 无目标）。"""
    for _name, cond, goal in CURRICULUM_RULES:
        try:
            if cond(state):
                return goal
        except Exception:
            continue
    return ""


class AutoCurriculum:
    """自动课程：周期对状态提议下一个目标。"""

    def __init__(self, config: dict = None):
        cfg = config or {}
        self.check_interval = cfg.get("check_interval", 30)
        self.enabled = cfg.get("enabled", True)

    def tick(self, state: dict) -> str:
        if not self.enabled:
            return ""
        return propose_goal(state)