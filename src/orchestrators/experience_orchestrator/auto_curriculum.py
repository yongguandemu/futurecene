"""auto_curriculum.py — 自动课程（游戏经验学习域）

Voyager 式：地面真值状态 + 知识库 rules → 下一个目标提议。规则模板驱动（零额外 LLM）。

# 模块内容清单 — auto_curriculum

## 1. 模块身份标识
- 所属调度官：experience
- 能力名：experience:curriculum

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| check_interval | 否 | 30 | int，>0 | 课程检查间隔（秒） |
| enabled | 否 | True | bool | 是否启用自动课程 |

## 3. 输入契约
- 输入格式：`tick(state)` -> str
- state：必填，dict，游戏状态（含 inventory/food 等字段）
- 纯函数：`propose_goal(state)` 也可直接调用

## 4. 输出契约
- 成功：返回下一个目标字符串（如 "gather_wood"），空串表示无目标
- 失败：异常时静默跳过，继续匹配下一条规则
- 事件：无

## 5. 依赖声明
- 外部服务：无
- 内部模块：无（纯规则引擎）
- 预先配置：无

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 规则异常 | cond(state) 抛异常 | 跳过该规则，继续匹配下一条 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start/stop | 否 | 无（纯状态快照，由 learn_brain 定时调用 tick） |

## 8. 领域状态说明
- 状态项：`check_interval`、`enabled`（无运行时状态）
- 持久化：无
- 恢复：构造即就绪
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