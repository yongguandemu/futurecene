"""game_operation_loop.py — 游戏操作全流程循环（P3 通用游戏操作核心）

识别 → 判断 → 操作 → 反馈 闭环：
- 感知（perceive）：注入的 perceive_fn 返回场景（OCR 文本/状态/选项/图像路径）
- 判断（decide）：状态机规则（对白停顿→推进、菜单→进入、选项→选择）+
  可选 LLM 决策 + 经验学习联动
- 操作（act）：注入的 act_fn 经 screen 调度官执行并广播虚拟光标
- 反馈（feedback）：操作后重新感知对比场景变化（OCR 文本 + 图像差分双通道）→
  熔断判定 + 经验记录

稳定与安全（承接原系统 game_operation_brain）：
- 使能状态机（GameOperationController）：AI 操作可启停、可到时自动停
- OperationSafety：熔断（连续无响应暂停）/ 防抖（同指令去重）/ 冷却（操作间隔）
- 重试机制：操作失败或场景无变化时按 retry_limit 重试，再计入连续失败
- 图像差分反馈：OCR 之外的第二反馈通道，画面变化（无文字游戏）也能感知
- 连续失败上限：超过 max_failures 自动停止，避免死循环

联动（全部经事件/注入，不直接 import 跨域模块）：
- 事件：game:op_state_changed / game:op_cycle / game:op_operation / game:op_feedback
- 决策日志：record_decision（executed / no_action / failed）
- 经验学习：经注入 experience_fn 记录操作经验（game:op_feedback 联动）
- 解说：经注入 commentary_fn 请求解说（可选）

# 模块内容清单 — game_operation_loop

## 1. 模块身份标识
- 所属调度官：game
- 能力名：game:op_start / game:op_stop / game:op_state / game:op_plan / game:op_command（承载实现）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| poll_interval | 否 | 2.0 | float 秒 | 感知轮询间隔 |
| advance_wait | 否 | 4.0 | float 秒 | 对白停顿多久后推进 |
| max_failures | 否 | 5 | int | 连续失败自动停止上限 |
| fuse_limit | 否 | 3 | int | 熔断阈值（转 OperationSafety） |
| fuse_pause | 否 | 60 | float 秒 | 熔断暂停时长 |
| dedup_window | 否 | 8 | float 秒 | 同指令去重窗口 |
| post_action_cooldown | 否 | 3.0 | float 秒 | 操作后冷却 |
| retry_limit | 否 | 2 | int | 操作失败/无变化重试上限 |
| image_diff_threshold | 否 | 0.02 | float | 图像差分变化判定阈值（像素占比） |

## 3. 输入契约
- 输入格式：`GameOperationLoop(controller, safety, perceive_fn, act_fn, event_bus=None, planner=None, experience_fn=None, commentary_fn=None, config=None)`
- perceive_fn：`fn() -> scene dict`（text/state/options/scene_type/image_path）
- act_fn：`fn(action, params) -> {"ok", "scene_changed"}`

## 4. 输出契约
- 成功：start()/stop()/snapshot()；循环内每轮发布 game:op_cycle
- 失败：感知/操作异常捕获记录，连续失败超限自动停止
- 事件：game:op_state_changed / game:op_cycle / game:op_operation / game:op_feedback
- 决策日志：record_decision（source=game_operation_loop）

## 5. 依赖声明
- 外部服务：无（感知/操作经注入回调）
- 内部模块：threading、time、logging、PIL（可选，图像差分）、src.shared.decision_log、src.shared.events
- 预先配置：GameOrchestrator 构造时创建并注入 screen 调度官回调

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 感知失败 | 截图/OCR 异常 | 捕获记录，本轮跳过，不熔断 |
| 操作失败 | 输入异常 | act_fn 返回 ok=False，按 retry_limit 重试后计入连续失败 |
| 连续失败超限 | 场景无变化 | 自动停止并发布 game:op_state_changed |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start | 是 | 启动感知-决策循环线程 |
| stop | 是 | 停止循环线程 |
| run_cycle | 是 | 单轮循环（测试可直接调用） |

## 8. 领域状态说明
- 状态项：_thread/_running/_last_scene_text/_scene_stable_since/_failures/_last_image_path
- 持久化：无
- 恢复：无状态持久化；start() 重建
"""
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from src.shared.decision_log import (
    OUTCOME_EXECUTED,
    OUTCOME_FAILED,
    OUTCOME_NO_ACTION,
    record_decision,
)
from src.shared.events import (
    GAME_OP_CYCLE,
    GAME_OP_FEEDBACK,
    GAME_OP_OPERATION,
    GAME_OP_STATE_CHANGED,
)

logger = logging.getLogger(__name__)

try:
    from PIL import Image, ImageChops
except ImportError:
    Image = None
    ImageChops = None


class GameOperationLoop:
    """游戏操作全流程循环：感知→判断→操作→反馈。"""

    def __init__(self, controller, safety, perceive_fn: Callable[[], Dict[str, Any]],
                 act_fn: Callable[[str, Dict[str, Any]], Dict[str, Any]],
                 event_bus=None, planner=None,
                 experience_fn: Optional[Callable] = None,
                 commentary_fn: Optional[Callable] = None,
                 config: Optional[Dict[str, Any]] = None):
        cfg = config or {}
        self._controller = controller
        self._safety = safety
        self._perceive_fn = perceive_fn
        self._act_fn = act_fn
        self._event_bus = event_bus
        self._planner = planner
        self._experience_fn = experience_fn
        self._commentary_fn = commentary_fn
        self._poll_interval = float(cfg.get("poll_interval", 2.0))
        self._advance_wait = float(cfg.get("advance_wait", 4.0))
        self._max_failures = int(cfg.get("max_failures", 5))
        self._retry_limit = int(cfg.get("retry_limit", 2))
        self._image_diff_threshold = float(cfg.get("image_diff_threshold", 0.02))
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.RLock()
        self._last_scene_text = ""
        self._scene_stable_since = 0.0
        self._failures = 0
        self._last_scene: Dict[str, Any] = {}
        self._last_image_path = ""

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="game-operation-loop")
        self._thread.start()
        self._publish_state("started")
        logger.info("[GameOperationLoop] 已启动")

    def stop(self) -> None:
        self._running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None
        self._publish_state("stopped")
        logger.info("[GameOperationLoop] 已停止")

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"running": self._running,
                    "failures": self._failures,
                    "last_scene": self._last_scene,
                    "controller": self._controller.status(),
                    "safety": self._safety.snapshot()}

    # ---------- 主循环 ----------

    def _loop(self) -> None:
        while self._running:
            try:
                self.run_cycle()
            except Exception as e:
                logger.error("[GameOperationLoop] 循环异常: %s", e)
            time.sleep(self._poll_interval)

    def run_cycle(self) -> Dict[str, Any]:
        """单轮循环：感知→判断→操作→反馈。返回本轮结果。"""
        if not self._controller.enabled:
            return {"action": "no_action", "reason": "disabled"}

        # 1. 感知
        scene = self._perceive_fn() or {}
        with self._lock:
            self._last_scene = scene
        text = (scene.get("text") or "").strip()
        state = scene.get("state") or "unknown"
        options = scene.get("options") or []

        # 2. 判断
        decision = self._decide(scene, text, state, options)
        action = decision.get("action")
        params = decision.get("params", {})
        reason = decision.get("reason", "")

        if action is None:
            record_decision(source="game_operation_loop", outcome=OUTCOME_NO_ACTION,
                            reason_code=reason or "no_action",
                            layer="L1", capability="game:op_loop",
                            detail="场景无需操作",
                            min_interval=self._poll_interval)
            return {"action": "no_action", "reason": reason}

        # 3. 安全判定（防抖/冷却/熔断）
        if not self._safety.allow(action):
            return {"action": "no_action", "reason": "safety_gate"}

        # 4. 操作（含重试）
        result, ok = self._act_with_retry(action, params)
        self._safety.mark_action(action)
        self._publish_operation(action, params, ok)

        # 5. 反馈：重新感知对比场景变化（文本 + 图像双通道）
        scene_changed = self._feedback(action, ok, scene)
        fused = self._safety.on_result(ok, scene_changed)

        record_decision(source="game_operation_loop", outcome=OUTCOME_EXECUTED,
                        reason_code="executed" if ok else "action_failed",
                        layer="L1", capability="game:op_loop",
                        detail="操作 {} {}".format(action, params),
                        min_interval=self._poll_interval)

        if not ok:
            with self._lock:
                self._failures += 1
                if self._failures >= self._max_failures:
                    self._controller.stop()
                    self._publish_state("stopped_by_failures")
                    logger.warning("[GameOperationLoop] 连续 %s 次失败，自动停止",
                                   self._failures)
        else:
            with self._lock:
                self._failures = 0

        # 6. 经验学习联动（操作成功且场景变化 → 记录经验）
        if ok and scene_changed and self._experience_fn is not None:
            try:
                self._experience_fn(action, params, scene)
            except Exception as e:
                logger.debug("[GameOperationLoop] 经验记录失败: %s", e)

        # 7. 解说联动（操作成功且场景变化 → 请求解说，指挥官编排 LLM+TTS）
        if ok and scene_changed and self._commentary_fn is not None:
            try:
                self._commentary_fn(action, scene)
            except Exception as e:
                logger.debug("[GameOperationLoop] 解说请求失败: %s", e)

        return {"action": action, "params": params, "ok": ok,
                "scene_changed": scene_changed, "fused": fused, "reason": reason}

    def _act_with_retry(self, action: str, params: Dict[str, Any]):
        """执行操作，失败或场景无变化时按 retry_limit 重试。返回 (result, ok)。"""
        result = self._act_fn(action, params) or {}
        ok = bool(result.get("ok", False))
        for attempt in range(self._retry_limit):
            if ok and result.get("scene_changed", True):
                break
            time.sleep(self._poll_interval * 0.5)
            result = self._act_fn(action, params) or {}
            ok = bool(result.get("ok", False))
            logger.debug("[GameOperationLoop] 操作 %s 重试 %s/%s (ok=%s)",
                         action, attempt + 1, self._retry_limit, ok)
        return result, ok

    # ---------- 判断 ----------

    def _decide(self, scene: Dict[str, Any], text: str, state: str,
                options: List[str]) -> Dict[str, Any]:
        """状态机决策：选项→选择；菜单→进入；对白停顿→推进。"""
        now = time.time()
        if options:
            choice = self._choose_option(options)
            return {"action": "select_option", "params": {"index": choice},
                    "reason": "options_present"}
        if state == "menu":
            return {"action": "advance", "params": {}, "reason": "menu_enter"}
        if state == "dialogue" and text:
            if text != self._last_scene_text:
                self._last_scene_text = text
                self._scene_stable_since = now
                return {"action": None, "reason": "scene_changed"}
            if now - self._scene_stable_since >= self._advance_wait:
                return {"action": "advance", "params": {}, "reason": "dialogue_pause"}
            return {"action": None, "reason": "dialogue_reading"}
        if state == "unknown" and not text:
            return {"action": None, "reason": "no_scene_text"}
        return {"action": None, "reason": "no_action"}

    def _choose_option(self, options: List[str]) -> int:
        """选项选择：默认第 0 个兜底（低成本确定性）。"""
        return 0

    # ---------- 反馈 ----------

    def _feedback(self, action: str, ok: bool,
                  before_scene: Dict[str, Any]) -> bool:
        """操作后重新感知，对比本轮操作前后场景是否变化（文本 + 图像双通道）。返回 scene_changed。"""
        if not ok:
            return False
        try:
            new_scene = self._perceive_fn() or {}
            new_text = (new_scene.get("text") or "").strip()
            before_text = (before_scene.get("text") or "").strip()
            text_changed = (new_text != before_text) or \
                           (new_scene.get("state") != before_scene.get("state"))
            image_changed = self._image_changed(
                before_scene.get("image_path"),
                new_scene.get("image_path"))
            changed = text_changed or image_changed
            if changed:
                self._last_scene_text = new_text
                self._scene_stable_since = time.time()
            with self._lock:
                self._last_scene = new_scene
            self._publish_feedback(action, ok, changed)
            return changed
        except Exception as e:
            logger.error("[GameOperationLoop] 反馈感知失败: %s", e)
            return False

    def _image_changed(self, old_path: Optional[str], new_path: Optional[str]) -> bool:
        """图像差分：两张截图差异像素占比超过阈值视为场景变化。"""
        if not old_path or not new_path or old_path == new_path:
            return False
        if Image is None or ImageChops is None:
            return False
        try:
            a = Image.open(old_path).convert("RGB")
            b = Image.open(new_path).convert("RGB")
            if a.size != b.size:
                b = b.resize(a.size)
            diff = ImageChops.difference(a, b)
            # diff.histogram() 为每通道 256 个 bin（R/G/B 顺序）
            hist = diff.histogram()
            total = a.size[0] * a.size[1]
            # 每通道差异 > 16 的像素数，取三通道最大值（任一通道变化即视为变化）
            per_channel = [sum(hist[c * 256 + 17:c * 256 + 256]) for c in range(3)]
            ratio = (max(per_channel) / total) if total else 0.0
            return ratio >= self._image_diff_threshold
        except Exception as e:
            logger.debug("[GameOperationLoop] 图像差分失败: %s", e)
            return False

    # ---------- 事件发布 ----------

    def _publish_state(self, state: str) -> None:
        if self._event_bus is not None:
            try:
                self._event_bus.publish(GAME_OP_STATE_CHANGED, state=state)
            except Exception as e:
                logger.debug("[GameOperationLoop] 状态事件发布失败: %s", e)

    def _publish_operation(self, action: str, params: Dict[str, Any],
                           ok: bool) -> None:
        if self._event_bus is not None:
            try:
                self._event_bus.publish(GAME_OP_OPERATION, action=action,
                                        params=params, ok=ok)
            except Exception as e:
                logger.debug("[GameOperationLoop] 操作事件发布失败: %s", e)

    def _publish_feedback(self, action: str, ok: bool, changed: bool) -> None:
        if self._event_bus is not None:
            try:
                self._event_bus.publish(GAME_OP_FEEDBACK, action=action,
                                        ok=ok, scene_changed=changed)
            except Exception as e:
                logger.debug("[GameOperationLoop] 反馈事件发布失败: %s", e)

    def _publish_cycle(self) -> None:
        if self._event_bus is not None:
            try:
                self._event_bus.publish(GAME_OP_CYCLE)
            except Exception as e:
                logger.debug("[GameOperationLoop] 周期事件发布失败: %s", e)
