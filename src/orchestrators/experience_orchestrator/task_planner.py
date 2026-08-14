"""task_planner.py — 任务分解器（游戏经验学习域）

GITM 式：目标 → 子任务队列 → 逐子任务执行。子任务完成检测用地面真值
（inventory/position）；LLM 拆解限流，rules 模板兜底（零额外 LLM）。

# 模块内容清单 — task_planner

## 1. 模块身份标识
- 所属调度官：experience
- 能力名：experience:plan

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| llm_plan_interval | 否 | 120 | float，>0 | LLM 拆解限流间隔（秒） |
| max_subtasks | 否 | 8 | int，>=1 | 子任务队列上限 |

## 3. 输入契约
- 输入格式：`plan(goal, state)` / `next_subtask()` / `mark_done()` / `is_complete(subtask, state)` / `current_goal()` / `reset()`
- goal：str，目标（命中 RULE_PLANS 模板则零 LLM 拆解）
- state：dict，游戏状态（含 inventory，兼容 mc.inventory 嵌套）
- subtask：dict，子任务（含 type/target/count）

## 4. 输出契约
- 成功：`plan()` 返回子任务 dict 列表；`next_subtask()` 返回队首子任务或 `None`；`mark_done()` 返回已完成的子任务或 `None`；`is_complete()` 返回 bool；`current_goal()` 返回 str；`reset()` 返回 `None`
- 失败：`plan()` 未命中模板且限流未过返回 `[]`；LLM 拆解异常返回 `[]`
- 事件：无

## 5. 依赖声明
- 外部服务：无（LLM 经 brain.generate_text 注入，未注入时返回 []）
- 内部模块：无（纯队列 + 规则模板）
- 预先配置：无

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| LLM 未注入 | brain 无 generate_text | 返回 []，走 rules 模板 |
| LLM 拆解异常 | 解析/调用异常 | 返回 []，记录警告 |
| 队列为空 | next_subtask/mark_done 时无子任务 | 返回 None |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start/stop | 否 | 无（构造即就绪） |
| reset | 是 | 清空子任务队列与当前目标 |

## 8. 领域状态说明
- 状态项：`_queue`（子任务队列）、`_goal`（当前目标）、`_last_llm`（LLM 限流时间戳）
- 持久化：无（内存态，重启丢失）
- 恢复：plan 重新生成子任务链
"""
import threading
import time
import logging

logger = logging.getLogger(__name__)

# 子任务类型
GATHER = "gather"
CRAFT = "craft"
MOVE = "move_to"
BUILD = "build"
REACH = "reach"

# 规则模板（零 LLM 兜底）：目标关键词 → 子任务链
RULE_PLANS = {
    "gather_wood": [
        {"type": GATHER, "target": "oak_log", "count": 4},
    ],
    "stone_pickaxe": [
        {"type": GATHER, "target": "oak_log", "count": 4},
        {"type": CRAFT, "target": "oak_planks", "count": 4},
        {"type": CRAFT, "target": "stick", "count": 4},
        {"type": GATHER, "target": "cobblestone", "count": 3},
        {"type": CRAFT, "target": "crafting_table", "count": 1},
        {"type": CRAFT, "target": "stone_pickaxe", "count": 1},
    ],
    "crafting_table": [
        {"type": GATHER, "target": "oak_log", "count": 4},
        {"type": CRAFT, "target": "crafting_table", "count": 1},
    ],
    "wooden_pickaxe": [
        {"type": GATHER, "target": "oak_log", "count": 4},
        {"type": CRAFT, "target": "oak_planks", "count": 4},
        {"type": CRAFT, "target": "stick", "count": 4},
        {"type": CRAFT, "target": "crafting_table", "count": 1},
        {"type": CRAFT, "target": "wooden_pickaxe", "count": 1},
    ],
    "wooden_axe": [
        {"type": GATHER, "target": "oak_log", "count": 4},
        {"type": CRAFT, "target": "oak_planks", "count": 4},
        {"type": CRAFT, "target": "stick", "count": 4},
        {"type": CRAFT, "target": "crafting_table", "count": 1},
        {"type": CRAFT, "target": "wooden_axe", "count": 1},
    ],
    "torch": [
        {"type": GATHER, "target": "oak_log", "count": 2},
        {"type": GATHER, "target": "coal", "count": 1},
        {"type": CRAFT, "target": "oak_planks", "count": 4},
        {"type": CRAFT, "target": "stick", "count": 4},
        {"type": CRAFT, "target": "torch", "count": 4},
    ],
    "gather_stone": [
        {"type": GATHER, "target": "cobblestone", "count": 4},
    ],
    "gather_food": [
        {"type": GATHER, "target": "apple", "count": 2},
    ],
}


class TaskPlanner:
    """目标 → 子任务队列（rules 模板优先，LLM 限流兜底）。"""

    def __init__(self, config: dict = None, brain=None):
        cfg = config or {}
        self.llm_interval = cfg.get("llm_plan_interval", 120)
        self.max_subtasks = cfg.get("max_subtasks", 8)
        self._queue = []
        self._goal = ""
        self._lock = threading.Lock()
        self._last_llm = 0.0
        # brain 引用（提供 generate_text 薄封装；未注入时 _llm_plan 返回 []）
        self._brain = brain

    def plan(self, goal: str, state: dict) -> list:
        """目标 → 子任务链（rules 模板优先；未命中且限流通过则 LLM）。"""
        with self._lock:
            self._goal = goal
        subtasks = RULE_PLANS.get(goal)
        if subtasks:
            with self._lock:
                self._queue = list(subtasks)[:self.max_subtasks]
            return self._queue
        now = time.time()
        if now - self._last_llm >= self.llm_interval:
            self._last_llm = now
            return self._llm_plan(goal, state)
        return []

    def _llm_plan(self, goal: str, state: dict) -> list:
        """LLM 拆解：brain 注入 generate_text 则调用，否则返回 []。"""
        try:
            brain = self._brain
            if brain is None or not callable(getattr(brain, "generate_text", None)):
                logger.info("[TaskPlanner] LLM 拆解跳过: brain 未注入 generate_text")
                return []
            inv = [i.get("name") for i in (state.get("inventory") or [])][:8]
            prompt = ("目标: {}，当前背包: {}。\n"
                      "拆解为 MC 子任务序列（每行一个: gather 物品 / craft 物品 / move_to x z），最多 6 步。"
                      .format(goal, "/".join(inv)))
            resp = (brain.generate_text(prompt, max_tokens=120) or "").strip()
            chain = []
            for line in resp.splitlines():
                line = line.strip()
                parts = line.split()
                if len(parts) >= 2 and parts[0] in ("gather", "craft"):
                    chain.append({"type": parts[0], "target": parts[1],
                                  "count": int(parts[2]) if len(parts) > 2 else 1})
                elif len(parts) >= 3 and parts[0] == "move_to":
                    chain.append({"type": MOVE, "x": parts[1], "z": parts[2]})
            with self._lock:
                self._queue = chain[:self.max_subtasks]
            logger.info("[TaskPlanner] LLM 计划 %s -> %s 子任务", goal, len(chain))
            return self._queue
        except Exception:
            logger.warning("[TaskPlanner] LLM 拆解失败", exc_info=True)
            return []

    def next_subtask(self):
        with self._lock:
            return self._queue[0] if self._queue else None

    def mark_done(self):
        with self._lock:
            if self._queue:
                done = self._queue.pop(0)
                logger.info("[TaskPlanner] 子任务完成: %s", done.get("type"))
                return done
        return None

    def is_complete(self, subtask: dict, state: dict) -> bool:
        """子任务完成检测（地面真值，兼容心跳 scene 嵌套）。"""
        inv = state.get("inventory")
        if inv is None:
            mc = state.get("mc") or {}
            inv = mc.get("inventory") if isinstance(mc, dict) else None
        counts = {}
        for i in (inv or []):
            if not isinstance(i, dict):
                continue
            counts[i.get("name")] = counts.get(i.get("name"), 0) + i.get("count", 0)
        t = subtask.get("target")
        if subtask.get("type") in (GATHER, CRAFT):
            return t in counts and counts[t] >= subtask.get("count", 1)
        return False

    def current_goal(self) -> str:
        with self._lock:
            return self._goal

    def reset(self):
        with self._lock:
            self._queue = []
            self._goal = ""