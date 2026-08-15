"""active_dialogue.py — 主动对话引擎（LLM 调度官子模块）

无用户输入时定时触发主动话题，冷场救星。定时线程 + 话题池 + 用户活动感知。
brain（LLM 文本生成函数）注入时优先生成，否则从内置话题池随机选取。
多角色协作（Task 18）：tick(role) 支持按角色生成（set_role_generator 注入
fn(role)->{text,mood}，角色化人设 + 对方最近发言），发布事件携带 role；
未指定角色时保持既有 set_generator 单角色行为（向后兼容）。

# 模块内容清单（8 项契约）
1. 模块身份标识：llm 调度官 · active_dialogue · 能力 llm:active_dialogue
2. 配置契约：min_interval(45) / max_interval(90) / min_cooldown(90) /
             max_silence(180) / trigger_probability(0.35) / enabled(True)
3. 输入契约：tick(role="") / notify_user_activity() / start() / stop() / get_status()
4. 输出契约：tick 返回 {"text":str,"mood":str} 或 None；发布 dialogue:active 事件（含 role）
5. 依赖声明：event_bus（可选）；llm 生成函数经 set_generator() 注入（可选）；
             角色化生成函数经 set_role_generator() 注入（可选，fn(role)->{text,mood}）
6. 错误定义：generator/role_generator 异常 → 回退话题池；event_bus 发布异常 → 记录日志不中断
7. 生命周期方法：start() 启动守护线程，stop() 置位并 join；幂等
8. 领域状态说明：_running / _last_active_time / _last_user_activity / _active_count
"""
import logging
import random
import threading
import time
from typing import Any, Callable, Dict, Optional

from src.shared.events import ACTIVE_DIALOGUE

logger = logging.getLogger(__name__)

# 内置主动话题池（brain/generator 不可用时的兜底语料）
DEFAULT_TOPICS = [
    {"text": "嗯…大家今天过得怎么样呀？有没有什么有趣的事情想分享呢？", "mood": "happy"},
    {"text": "说起来，我最近在想一个问题，你们觉得什么才是真正重要的呢？", "mood": "curious"},
    {"text": "好久没和大家聊天了，有点想你们呢~", "mood": "shy"},
    {"text": "今晚的夜色真美，适合说说话呢。你们那边的天气怎么样？", "mood": "calm"},
    {"text": "突然想起来一个有趣的故事，要听我讲讲吗？", "mood": "happy"},
    {"text": "诶，有没有人推荐一下最近好听的音乐呀？", "mood": "curious"},
    {"text": "时间过得好快，不知不觉已经聊了这么久了呢。", "mood": "calm"},
    {"text": "大家有没有什么想问我的？什么都可以问哦~", "mood": "happy"},
    {"text": "你们平时无聊的时候都会做些什么呀？我好奇~", "mood": "curious"},
    {"text": "今天遇到了一件小事，让我感触挺深的，想和你们说说。", "mood": "calm"},
]


class ActiveDialogue:
    """主动对话引擎：定时检查冷场状态并生成主动发言。"""

    def __init__(self, event_bus=None, config: dict = None):
        cfg = config or {}
        self._bus = event_bus
        self.min_interval = float(cfg.get("min_interval", 45.0))
        self.max_interval = float(cfg.get("max_interval", 90.0))
        self.min_cooldown = float(cfg.get("min_cooldown", 90.0))
        self.max_silence = float(cfg.get("max_silence", 180.0))
        self.trigger_probability = float(cfg.get("trigger_probability", 0.35))
        self.enabled = bool(cfg.get("enabled", True))

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._generator: Optional[Callable[[], Any]] = None
        self._role_generator: Optional[Callable[[str], Any]] = None
        self._role_provider: Optional[Callable[[], str]] = None
        self._last_active_time = 0.0
        self._last_user_activity = time.time()
        self._active_count = 0
        self._last_result: Optional[Dict[str, str]] = None
        self._danmaku_subscribed = False
        logger.info("[ActiveDialogue] 初始化完成 (enabled=%s)", self.enabled)

    # ---------- 接入 ----------

    def set_generator(self, fn: Callable[[], Any]):
        """注入主动话题生成函数（orchestrator 接线，避免直接依赖）。"""
        self._generator = fn

    def set_role_generator(self, fn: Callable[[str], Any]):
        """注入按角色生成话题的函数（fn(role) -> {text, mood}）。

        多角色协作（Task 18）使用：tick(role) 指定角色时优先调用本函数生成
        角色化话题（人设 + 对方最近发言），失败回退话题池；与 set_generator
        并存——role 为空时保持既有 set_generator 行为（单角色兼容）。
        """
        self._role_generator = fn

    def set_role_provider(self, fn: Callable[[], str]):
        """注入当前角色获取函数（fn() -> role_str），供 _timer_loop 自动传 role。"""
        self._role_provider = fn

    def set_event_bus(self, event_bus):
        self._bus = event_bus

    # ---------- 生命周期 ----------

    def start(self):
        if not self.enabled:
            logger.info("[ActiveDialogue] 已禁用，不启动")
            return
        if self._bus is not None and not self._danmaku_subscribed:
            try:
                from src.shared.events import DANMAKU_RECEIVED
                self._bus.subscribe(DANMAKU_RECEIVED,
                                    lambda *a, **kw: self.notify_user_activity(),
                                    priority=80)
                self._danmaku_subscribed = True
            except Exception as e:
                logger.warning("[ActiveDialogue] 弹幕订阅失败: %s", e)
        with self._lock:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(target=self._timer_loop, daemon=True,
                                        name="active-dialogue")
        self._thread.start()
        logger.info("[ActiveDialogue] 引擎已启动 (间隔 %.0f-%.0fs, 冷却 %.0fs)",
                    self.min_interval, self.max_interval, self.min_cooldown)

    def stop(self):
        with self._lock:
            if not self._running:
                return
            self._running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._thread = None
        logger.info("[ActiveDialogue] 引擎已停止 (共 %d 次)", self._active_count)

    # ---------- 触发 ----------

    def tick(self, role: str = "") -> Optional[Dict[str, str]]:
        """触发一次主动对话检查。满足条件则生成并发布，否则返回 None。

        role 非空时按角色生成（多角色协作：role_generator 优先），发布事件
        携带 role；role 为空时沿用既有 generator/话题池（单角色兼容）。
        """
        now = time.time()
        if self._last_active_time > 0 and now - self._last_active_time < self.min_cooldown:
            return None
        silent = now - self._last_user_activity
        if silent < self.max_silence and random.random() > self.trigger_probability:
            return None
        result = self._generate(role)
        if not result or not result.get("text"):
            return None
        text = result["text"].strip()
        mood = result.get("mood", "default")
        if not text:
            return None
        self._last_active_time = now
        self._active_count += 1
        self._last_result = {"text": text, "mood": mood}
        self._publish(ACTIVE_DIALOGUE, text=text, mood=mood, role=role,
                      source="active_dialogue", timestamp=now, count=self._active_count)
        logger.info("[ActiveDialogue] 主动对话 #%d [%s] role=%s: %s",
                    self._active_count, mood, role or "default", text[:60])
        return {"text": text, "mood": mood}

    def notify_user_activity(self):
        """记录用户活动，重置静默计时。"""
        self._last_user_activity = time.time()

    def get_status(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "running": self._running,
            "enabled": self.enabled,
            "active_count": self._active_count,
            "last_active_time": self._last_active_time,
            "seconds_since_last": (now - self._last_active_time)
            if self._last_active_time else -1,
            "seconds_since_user": now - self._last_user_activity,
        }

    # ---------- 内部 ----------

    def _timer_loop(self):
        while self._running:
            interval = random.uniform(self.min_interval, self.max_interval)
            self._sleep_interruptible(interval)
            if not self._running:
                break
            try:
                role = self._role_provider() if self._role_provider else ""
                self.tick(role=role)
            except Exception as e:
                logger.error("[ActiveDialogue] tick 异常: %s", e)

    def _generate(self, role: str = "") -> Optional[Dict[str, str]]:
        """生成主动话题：role 非空优先 role_generator(role)，否则 generator()，最后话题池。"""
        if role and self._role_generator is not None:
            try:
                parsed = self._parse_generator_result(self._role_generator(role))
                if parsed:
                    return parsed
            except Exception as e:
                logger.warning("[ActiveDialogue] role_generator 失败，回退话题池: %s", e)
        if self._generator is not None:
            try:
                parsed = self._parse_generator_result(self._generator())
                if parsed:
                    return parsed
            except Exception as e:
                logger.warning("[ActiveDialogue] generator 失败，回退话题池: %s", e)
        return random.choice(DEFAULT_TOPICS)

    @staticmethod
    def _parse_generator_result(result: Any) -> Optional[Dict[str, str]]:
        if result is None:
            return None
        if isinstance(result, dict):
            text = result.get("text") or result.get("content") or result.get("reply") or ""
            mood = result.get("mood") or result.get("emotion") or "default"
            return {"text": str(text), "mood": str(mood)} if text else None
        if isinstance(result, str):
            text = result.strip()
            return {"text": text, "mood": "default"} if text else None
        return None

    def _publish(self, event: str, **data):
        if self._bus is None:
            return
        try:
            self._bus.publish(event, **data)
        except Exception as e:
            logger.error("[ActiveDialogue] 发布事件 %s 失败: %s", event, e)

    def _sleep_interruptible(self, seconds: float):
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(0.5, end - time.time()))