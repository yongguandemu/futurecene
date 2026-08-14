"""learn_brain.py — 经验学习决策器（游戏经验学习域）

经验检索优先 → 种子规则回退 → LLM 探索。决策后经桥执行，反馈回写经验库
（跨会话学习）。游戏无关：动作集来自原语注册表。

# 模块内容清单 — learn_brain

## 1. 模块身份标识
- 所属调度官：experience
- 能力名：experience:decide

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| operation_check_interval | 否 | 4.0 | float，>0 | 决策循环心跳间隔（秒） |
| post_action_cooldown | 否 | 6.0 | float，>0 | 动作后冷却（秒） |
| explore_llm_interval | 否 | 300 | float，>0 | LLM 探索限流间隔（秒） |
| no_change_retry | 否 | 2 | int，>=1 | 无变化重试次数（达阈值记失败） |
| feedback_change_threshold | 否 | 0.08 | float | 反馈变化阈值 |
| fuse_limit | 否 | 3 | int，>=1 | 连续失败熔断阈值 |
| fuse_pause | 否 | 60 | float，>0 | 熔断暂停时长（秒） |
| fix_interval | 否 | 120 | float，>0 | 修正重试间隔（秒） |
| goal_timeout | 否 | 180 | float，>0 | 外部任务超时（秒） |
| goal_rules_file | 否 | data/goal_rules.json | str | 目标经验规则文件 |
| goal_decide_interval | 否 | 30 | float，>0 | 总脑目标决策间隔（秒） |
| planner_enabled | 否 | True | bool | 是否启用任务分解器 |
| curriculum_enabled | 否 | True | bool | 是否启用自动课程 |
| game | 否 | "" | str | 游戏名（加载知识库） |
| data_file/min_confidence/query_top_k/max_entries/save_throttle | 否 | 见 store | - | 透传给 ExperienceStore |

## 3. 输入契约
- 输入格式：`decide(state, scene)`（内部循环调用）/ `on_feedback(state_changed, event_positive, error_context)` / `inject_task(goal)` / `on_user_correct()` / `set_llm_fn(fn)` / `set_brain(brain)` / `set_bus(event_bus)`
- state：GameState；scene：dict（含 text/options/state）
- goal：str，外部任务目标（非空才注入）
- 事件订阅：`game:goal_received`（经 _make_goal_handler 回调 inject_task）

## 4. 输出契约
- 成功：`inject_task()` 返回 bool；`stats()` 返回 dict；`goal_adjustments()` 返回 dict
- 失败：`inject_task()` goal 为空返回 `False`
- 事件：发布 `EXPERIENCE_RECORDED / EXPERIENCE_QUERIED / EXPERIENCE_GOAL_COMPLETED`
- 动作下发：经 adapter `_push_operation(action, args, bridge="mc")`

## 5. 依赖声明
- 外部服务：无（LLM 经 brain.generate_text / llm_fn 注入，不直接依赖）
- 内部模块：`experience_store.ExperienceStore`、`state_encoder.GameState`、`task_planner.TaskPlanner`、`auto_curriculum.AutoCurriculum`、`primitives`、`game_registry`、`src/shared/events`
- 预先配置：adapter 必须提供 last_scene / feedback_from_heartbeat / _push_operation（或由 orchestrator 注入）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 决策循环异常 | 单次心跳异常 | 记录警告，继续下一轮 |
| LLM 调用失败 | brain/llm_fn 异常 | 返回空串，走规则兜底 |
| 动作下发失败 | adapter 无 _push_operation | 返回 False，不阻断 |
| 目标规则读写失败 | goal_rules.json 损坏 | 清空 _goal_adjust，记录警告 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start | 是 | 启动决策循环线程（daemon） |
| stop | 是 | 置停止标记 + flush 经验库落盘 |

## 8. 领域状态说明
- 状态项：`_thread/_stop`（循环线程）、`_last_action/_last_state`（最近动作与状态）、`_fuse_count/_fuse_until`（熔断）、`_no_change_count`、`_goal_adjust`（目标经验规则）、`_external_goal*`（外部任务）、`_store`（经验库）
- 持久化：经验库 + 目标规则（本地 json，stop 时 flush）
- 恢复：start 重建线程；经验跨会话累积
"""
import threading
import time
import logging

from src.orchestrators.experience_orchestrator.experience_store import ExperienceStore
from src.orchestrators.experience_orchestrator.state_encoder import GameState
from src.orchestrators.experience_orchestrator.task_planner import TaskPlanner
from src.orchestrators.experience_orchestrator.auto_curriculum import AutoCurriculum
from src.orchestrators.experience_orchestrator import primitives
from src.orchestrators.experience_orchestrator import game_registry
from src.shared.decision_log import (
    OUTCOME_FAILED, OUTCOME_NO_ACTION, record_decision,
)
from src.shared.events import (
    EXPERIENCE_RECORDED, EXPERIENCE_QUERIED, EXPERIENCE_GOAL_COMPLETED,
)

logger = logging.getLogger(__name__)


class ExperienceLearnBrain:
    """经验驱动决策器（取代 if-else 核心，保留种子规则）。"""

    def __init__(self, adapter, config: dict = None, event_bus=None):
        self.adapter = adapter
        cfg = config or {}
        self.check_interval = cfg.get("operation_check_interval", 4.0)
        self.post_action_cooldown = cfg.get("post_action_cooldown", 6.0)
        self.explore_llm_interval = cfg.get("explore_llm_interval", 300)
        self.no_change_retry = cfg.get("no_change_retry", 2)
        self.change_threshold = cfg.get("feedback_change_threshold", 0.08)
        self._stop = threading.Event()
        self._thread = None
        self._last_action_time = 0.0
        self._last_action = None
        self._last_state = None
        self._last_scene_text = ""
        self._scene_stable_since = 0.0
        self._fuse_count = 0
        self._fuse_until = 0.0
        self.fuse_limit = cfg.get("fuse_limit", 3)
        self.fuse_pause = cfg.get("fuse_pause", 60)
        self._no_change_count = 0
        self._last_explore_llm = 0.0
        self.game = cfg.get("game", "")
        self._knowledge = None
        if self.game:
            try:
                self._knowledge = game_registry.get_game(self.game)
            except Exception:
                self._knowledge = None
        self._planner = None
        self._curriculum = None
        if cfg.get("planner_enabled", True):
            self._planner = TaskPlanner(cfg)
        if cfg.get("curriculum_enabled", True):
            self._curriculum = AutoCurriculum(cfg)
        self._curriculum_tick = 0.0
        self._store = ExperienceStore(
            data_file=cfg.get("data_file"), game=cfg.get("game"),
            min_confidence=cfg.get("min_confidence", 0.7),
            query_top_k=cfg.get("query_top_k", 3),
            max_entries=cfg.get("max_entries", 5000),
            save_throttle=cfg.get("save_throttle", 5.0))
        self.fix_interval = cfg.get("fix_interval", 120)
        self._last_fix = 0.0
        self._fix_history = {}
        # 外部任务注入（执行器化）：game:goal_received → inject_task
        self._external_goal_active = False
        self._external_goal = ""
        self._external_goal_ts = 0.0
        self._external_goal_timeout = cfg.get("goal_timeout", 180)
        # 目的驱动：层2 经验规则 + 层3 总脑触发
        self.brain = None
        self._goal_adjust = {}
        self._goal_rules_file = cfg.get("goal_rules_file", "data/goal_rules.json")
        self._last_goal_decide = 0.0
        self._goal_decide_interval = cfg.get("goal_decide_interval", 30)
        self._last_death_count = 0
        self._load_goal_rules()

        # 事件总线
        self._bus = event_bus
        self._on_goal_received = _make_goal_handler(self)

    def set_bus(self, event_bus):
        """注入事件总线（测试/接线用）。"""
        self._bus = event_bus

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="experience-learn-brain")
        self._thread.start()
        logger.info("[LearnBrain] 经验学习决策循环已启动")

    def stop(self):
        self._stop.set()
        try:
            self._store.flush()
        except Exception:
            pass

    def inject_task(self, goal: str) -> bool:
        """外部任务注入：设置外部任务标志 + 触发 planner 规划。"""
        goal = (goal or "").strip()
        if not goal:
            logger.warning("[LearnBrain] 外部任务注入失败: goal 为空")
            record_decision(source="learn_brain", outcome=OUTCOME_NO_ACTION,
                            reason_code="empty_goal",
                            layer="L3", capability="experience:inject_task",
                            detail="外部任务注入 goal 为空，拒绝",
                            min_interval=30)
            return False
        scene = self.adapter.last_scene if hasattr(self.adapter, "last_scene") else {}
        st = scene.get("state") or {}
        self._external_goal_active = True
        self._external_goal = goal
        self._external_goal_ts = time.time()
        if self._planner is not None:
            self._planner.plan(goal, st)
        logger.info("[LearnBrain] 外部任务注入: %s (子任务=%s)", goal,
                    self._planner.next_subtask() if self._planner is not None else None)
        return True

    # ========== 目的驱动：层2 经验规则（学习产生） ==========

    def set_brain(self, brain):
        self.brain = brain

    def goal_adjustments(self) -> dict:
        return dict(self._goal_adjust)

    def _record_goal_feedback(self, goal: str, outcome: str, reason: str = ""):
        if not goal:
            return
        cur = self._goal_adjust.get(goal, {"delta": 0.0, "reason": "", "ts": 0.0})
        cur["delta"] = float(cur.get("delta", 0.0) or 0.0)
        if outcome == "success":
            cur["delta"] = min(0.8, cur["delta"] + 0.1)
            cur["reason"] = reason or "执行成功"
        elif outcome == "death":
            cur["delta"] = max(-0.4, cur["delta"] - 0.3)
            cur["reason"] = reason or "死亡教训"
        else:
            cur["delta"] = max(-0.4, cur["delta"] - 0.2)
            cur["reason"] = reason or "执行失败"
        cur["ts"] = time.time()
        self._goal_adjust[goal] = cur
        self._save_goal_rules()
        logger.info("[LearnBrain] 经验规则更新: %s %s (delta=%.2f, reason=%s)",
                    goal, outcome, cur["delta"], cur["reason"])

    def _load_goal_rules(self):
        import json
        import os
        try:
            with open(self._goal_rules_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            data = data if isinstance(data, dict) else {}
            now = time.time()
            half = 1800
            for g, r in data.items():
                if isinstance(r, dict) and r.get("ts"):
                    age = now - float(r.get("ts", 0))
                    if age > 0:
                        decay = 0.5 ** (age / half)
                        r["delta"] = float(r.get("delta", 0.0) or 0.0) * decay
            self._goal_adjust = data
        except Exception:
            self._goal_adjust = {}

    def _save_goal_rules(self):
        import json
        import os
        try:
            d = os.path.dirname(self._goal_rules_file)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            with open(self._goal_rules_file, "w", encoding="utf-8") as f:
                json.dump(self._goal_adjust, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _trigger_goal_decide(self):
        if self.brain is None or self._external_goal_active:
            return
        if not callable(getattr(self.brain, "decide_goal", None)):
            return
        now = time.time()
        if now - self._last_goal_decide < self._goal_decide_interval:
            return
        self._last_goal_decide = now
        try:
            self.brain.decide_goal()
        except Exception as e:
            logger.warning("[LearnBrain] decide_goal 调用异常: %s", e)

    def _loop(self):
        n = 0
        while not self._stop.wait(self.check_interval):
            n += 1
            try:
                scene = (self.adapter.last_scene if hasattr(self.adapter, "last_scene")
                         else {}) or {}
                # 阶段1 学习闭环：心跳 scene 对比反馈（外部任务注入期间跳过）
                if not self._external_goal_active:
                    fb_holder = getattr(self.adapter, "feedback_from_heartbeat", None)
                    if fb_holder:
                        f = fb_holder() or {}
                        self.on_feedback(f.get("state_changed", False),
                                         f.get("event_positive", False),
                                         f.get("error_context", ""))
                    dc = (((scene.get("state") or {}).get("mc") or {})
                          .get("death_count", 0) or 0)
                    if dc > self._last_death_count:
                        cur_goal = ""
                        if self._planner is not None:
                            cur_goal = self._planner.current_goal()
                        self._record_goal_feedback(cur_goal, "death",
                                                   "death_count=%s" % dc)
                    self._last_death_count = dc
                    self._trigger_goal_decide()
                state = GameState(
                    scene_type=(scene.get("state") or {}).get("scene_type", "unknown"),
                    text=(scene.get("text") or ""),
                    fingerprint=(scene.get("state") and scene["state"]
                                 .get("fingerprint", "")) or "",
                    timestamp=time.time())
                # 自动课程 → 目标 → 计划（外部任务优先）
                if not self._external_goal_active and self._curriculum is not None and \
                        time.time() - self._curriculum_tick >= self._curriculum.check_interval:
                    self._curriculum_tick = time.time()
                    st = scene.get("state") or {}
                    goal = self._curriculum.tick(st)
                    if goal and self._planner is not None:
                        self._planner.plan(goal, st)
                # 子任务推进
                if self._planner is not None:
                    st = scene.get("state") or {}
                    cur = self._planner.next_subtask()
                    if cur is not None and self._planner.is_complete(cur, st):
                        self._planner.mark_done()
                # 外部任务完成检测
                if self._external_goal_active and self._planner is not None and \
                        self._planner.next_subtask() is None:
                    done_goal = self._external_goal
                    self._external_goal_active = False
                    self._external_goal = ""
                    logger.info("[LearnBrain] 外部任务完成: %s", done_goal)
                    self._publish_goal(done_goal, True)
                elif self._external_goal_active and \
                        time.time() - self._external_goal_ts > self._external_goal_timeout:
                    done_goal = self._external_goal
                    self._external_goal_active = False
                    self._external_goal = ""
                    logger.warning("[LearnBrain] 外部任务超时释放: %s (>%ss)",
                                   done_goal, self._external_goal_timeout)
                    self._publish_goal(done_goal, False)
                self._decide(state, scene)
            except Exception:
                logger.warning("[LearnBrain] 决策异常", exc_info=True)

    def _publish_goal(self, goal: str, ok: bool):
        if self._bus is None:
            return
        try:
            self._bus.publish(EXPERIENCE_GOAL_COMPLETED, goal=goal, ok=ok)
        except Exception:
            pass

    def _decide(self, state: GameState, scene: dict):
        now = time.time()
        if now < self._fuse_until:
            record_decision(source="learn_brain", outcome=OUTCOME_NO_ACTION,
                            reason_code="fuse_paused",
                            layer="L1", capability="experience:decide",
                            detail="熔断中，剩余 {:.0f}s".format(self._fuse_until - now),
                            min_interval=60)
            return
        if now - self._last_action_time < self.post_action_cooldown:
            record_decision(source="learn_brain", outcome=OUTCOME_NO_ACTION,
                            reason_code="post_action_cooldown",
                            layer="L1", capability="experience:decide",
                            detail="动作冷却中，剩余 {:.0f}s".format(
                                self.post_action_cooldown - (now - self._last_action_time)),
                            min_interval=30)
            return
        hits = self._store.query(state)
        if hits:
            rec, sim = hits[0]
            logger.info("[LearnBrain] 命中经验: %s (sim=%.2f, conf=%.2f)",
                        rec["action"], sim, rec["confidence"])
            self._publish_query(state, rec, sim)
            self._push(rec["action"], rec["args"] or {})
            self._last_state = state
            self._last_action = rec["action"]
            self._last_action_time = now
            return
        # 1.5 复合技能
        cond = {"type": state.scene_type}
        skills = self._store.query_skill(cond)
        if skills:
            rec, _sc = skills[0]
            for step in rec["steps"]:
                self._push(step.get("action"), step.get("args") or {})
            logger.info("[LearnBrain] 命中复合技能: %s (%s步)",
                        rec["skill"], len(rec["steps"]))
            self._last_state = state
            self._last_action = rec["skill"]
            self._last_action_time = now
            return
        action = self._rule_decide(scene)
        if action:
            self._push(action[0], action[1])
            self._last_state = state
            self._last_action = action[0]
            self._last_action_time = now
            return
        # 3.5 子任务引导动作
        if self._planner is not None and self._planner.next_subtask() is not None:
            cur = self._planner.next_subtask()
            if cur and cur.get("type") == "craft":
                item = str(cur.get("target") or "")
                if item:
                    pushed = self._push("craft_item",
                                        {"item": item,
                                         "count": int(cur.get("count", 1) or 1)})
                    if pushed:
                        self._last_state = state
                        self._last_action = "craft_item"
                        self._last_action_time = now
                        return
            if cur and cur.get("type") == "gather":
                import random
                st = scene.get("state") or {}
                pos = (st.get("mc") or {}).get("position") or {"x": 0, "z": 0}
                tx = int(pos.get("x", 0)) + random.choice([-25, -15, 15, 25])
                tz = int(pos.get("z", 0)) + random.choice([-25, -15, 15, 25])
                self._push("move_to", {"x": tx, "z": tz})
                self._last_state = state
                self._last_action = "move_to"
                self._last_action_time = now
                return
        # 3. LLM 探索（限流；有子任务时加速到 30s）
        subtask_active = (self._planner is not None and
                          self._planner.next_subtask() is not None)
        interval = min(self.explore_llm_interval, 30) if subtask_active \
            else self.explore_llm_interval
        if now - self._last_explore_llm >= interval:
            proposal = self._explore_llm(scene)
            if proposal:
                if proposal[0] == "get_state":
                    return
                self._last_explore_llm = now
                self._push(proposal[0], proposal[1])
                self._last_state = state
                self._last_action = proposal[0]
                self._last_action_time = now
        # 走到这里 = 本轮未下发任何动作，显式记录「决定不动作」
        record_decision(source="learn_brain", outcome=OUTCOME_NO_ACTION,
                        reason_code="no_candidate_action",
                        layer="L1", capability="experience:decide",
                        detail="经验/技能/规则/子任务/LLM 均无候选动作",
                        min_interval=30)

    def _rule_decide(self, scene: dict):
        text = (scene.get("text") or "").strip()
        options = scene.get("options") or []
        stype = (scene.get("state") or {}).get("scene_type", "")
        if options:
            return ("select_option", {"index": 0})
        if stype == "menu":
            return ("press_key", {"vk": 0x0D})
        if text and text != self._last_scene_text:
            self._last_scene_text = text
            self._scene_stable_since = time.time()
            return None
        if text and time.time() - self._scene_stable_since >= 4.0:
            return ("press_key", {"vk": 0x0D})
        return None

    def _explore_llm(self, scene: dict):
        try:
            stype = (scene.get("state") or {}).get("scene_type", "unknown")
            text = (scene.get("text") or "")[:60]
            subtask = ""
            if self._planner is not None:
                cur = self._planner.next_subtask()
                if cur:
                    subtask = "当前子任务: {}".format(cur.get("target") or cur.get("type"))
            kb = ""
            if self._knowledge is not None:
                kb = self._knowledge.inject_decision()
                if kb:
                    doc = self._knowledge.search_docs(text)
                    if doc:
                        kb += "\n" + doc
            prompt = ("当前游戏场景:{} 文本:{}。{}\n{}\n"
                      "建议一个游戏操作（仅输出动作名+参数JSON，可选动作:{}，"
                      "如 press_key {{\"vk\":32}} 空格）。"
                      .format(stype, text, subtask, kb,
                              "/".join(primitives.actions())))
            resp = self._llm_text(prompt, max_tokens=40)
            return self._parse_proposal(resp)
        except Exception:
            return None

    def _llm_text(self, prompt: str, max_tokens: int = 40) -> str:
        """轻量 LLM 文本生成：优先 brain.generate_text，其次 llm 调度官注入器。"""
        brain = getattr(self, "brain", None)
        if brain is not None and callable(getattr(brain, "generate_text", None)):
            try:
                return brain.generate_text(prompt, max_tokens=max_tokens) or ""
            except Exception:
                return ""
        # 注入的 llm 处理函数（由 orchestrator 接线提供）
        llm_fn = getattr(self, "_llm_fn", None)
        if llm_fn is not None:
            try:
                return llm_fn(prompt, max_tokens=max_tokens) or ""
            except Exception:
                return ""
        return ""

    def set_llm_fn(self, fn):
        """注入 LLM 文本生成函数（orchestrator 接线，避免直接依赖 llm 调度官）。"""
        self._llm_fn = fn

    @staticmethod
    def _parse_proposal(resp: str) -> tuple:
        import re
        if not resp:
            return None
        m = re.search(r'(\w+)\s*(\{.*?\})', resp, re.S)
        if not m:
            return None
        action, args_str = m.group(1), m.group(2)
        try:
            import json
            args = json.loads(args_str)
        except Exception:
            args = {}
        ok, _err = primitives.validate_action(action, args)
        if ok:
            return (action, args)
        return None

    def _push(self, action: str, args: dict) -> bool:
        push = getattr(self.adapter, "_push_operation", None)
        if callable(push):
            try:
                ok = push(action, args, bridge="mc")
            except Exception:
                ok = False
        else:
            ok = False
        if ok:
            logger.info("[LearnBrain] 下发操作: %s %s", action, args)
        else:
            record_decision(source="learn_brain", outcome=OUTCOME_FAILED,
                            reason_code="push_failed",
                            layer="L1", capability="experience:decide",
                            detail="动作下发失败: {} (adapter 无 _push_operation 或异常)".format(action),
                            min_interval=60)
        return ok

    def on_feedback(self, state_changed: bool, event_positive: bool = False,
                    error_context: str = ""):
        if not self._last_action:
            return
        if state_changed or event_positive:
            outcome = "success"
            self._no_change_count = 0
            self._fuse_count = 0
        else:
            self._no_change_count += 1
            outcome = ("failure" if self._no_change_count >= self.no_change_retry
                       else "no_change")
        if self._last_state:
            self._store.record(self._last_state, self._last_action, {}, outcome)
            self._publish_recorded(self._last_state, self._last_action, outcome)
            if outcome == "failure":
                self._fuse_count += 1
                if self._fuse_count >= self.fuse_limit:
                    self._fuse_until = time.time() + self.fuse_pause
                    self._fuse_count = 0
                self._try_fix(error_context)
                logger.info("[LearnBrain] 经验回写: %s → %s", self._last_action, outcome)
            try:
                cur_goal = ""
                if self._planner is not None:
                    cur_goal = self._planner.current_goal()
                if not cur_goal:
                    cur_goal = self._external_goal
                self._record_goal_feedback(cur_goal, outcome, error_context)
            except Exception:
                pass

    def _publish_recorded(self, state: GameState, action: str, outcome: str):
        if self._bus is None:
            return
        try:
            self._bus.publish(EXPERIENCE_RECORDED, action=action, outcome=outcome,
                              scene_type=state.scene_type)
        except Exception:
            pass

    def _publish_query(self, state: GameState, rec: dict, sim: float):
        if self._bus is None:
            return
        try:
            self._bus.publish(EXPERIENCE_QUERIED, action=rec.get("action"),
                              similarity=round(sim, 3), scene_type=state.scene_type)
        except Exception:
            pass

    def _try_fix(self, error_context: str):
        now = time.time()
        if now - self._last_fix < self.fix_interval or not error_context:
            return
        if not self._last_state or not self._last_action:
            return
        self._last_fix = now
        try:
            prompt = ("操作 {} 失败。错误: {} 状态: {}。\n"
                      "给出修正后的动作参数 JSON（仅参数，如 {{\"x\":10,\"z\":20}}）。"
                      .format(self._last_action, error_context[:80],
                              (self._last_state.text or "")[:50]))
            resp = (self._llm_text(prompt, max_tokens=40) or "").strip()
            import json
            import re
            m = re.search(r'(\{.*?\})', resp, re.S)
            if not m:
                return
            args = json.loads(m.group(1))
            ok = False
            push = getattr(self.adapter, "_push_operation", None)
            if callable(push):
                try:
                    ok = push(self._last_action, args, bridge="mc")
                except Exception:
                    ok = False
            if ok:
                self._fix_history[self._last_action] = {"args": args, "ts": now}
                logger.info("[LearnBrain] 修正重试: %s %s", self._last_action, args)
        except Exception:
            logger.debug("[LearnBrain] 修正失败", exc_info=True)

    def on_user_correct(self):
        if self._last_state and self._last_action:
            self._store.record(self._last_state, self._last_action, {}, "failure")
            logger.info("[LearnBrain] 人工纠正: %s 标记失败", self._last_action)

    def stats(self) -> dict:
        return self._store.stats()


def _make_goal_handler(brain):
    """构造 game:goal_received 订阅回调（字符串契约防御）。"""
    def _on_goal_received(event="", **data):
        goal = (data or {}).get("goal")
        if isinstance(goal, dict):
            goal = goal.get("action") or goal.get("name") or goal.get("goal") or ""
        if goal and isinstance(goal, str):
            logger.info("[LearnBrain] 收到外部任务事件 game:goal_received: %s", goal)
            brain.inject_task(goal)
        elif goal:
            logger.warning("[LearnBrain] 忽略非字符串 goal: %r", goal)
    return _on_goal_received