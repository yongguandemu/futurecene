"""模块内容清单 — danmaku_reactor

## 1. 模块身份标识
- 所属调度官：live_intelligence_orchestrator
- 能力名：intel:react / react_stats
- 引擎名：（单实现）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| cooldowns | 否 | 见 DEFAULT_COOLDOWNS | dict[str,float] 秒 | 各情感冷却时间 |
| global_cooldown | 否 | 1.0 | float 秒 | 任意回复最小间隔 |
| rate_max | 否 | 20 | int 次/分钟 | 全局速率上限 |

## 3. 输入契约
- intel:react 输入：{"text": str, "user"?: str}
  - text 必填，str，非空
  - user 可选，str，默认 "观众"
- react_stats 输入：无

## 4. 输出契约
- 成功：{"ok": true, "data": {"reply": str|None, "sentiment": str}, "error": null}
- 失败：{"ok": false, "data": {}, "error": str}

## 5. 依赖声明
- 外部服务：无（情感分析器可选注入，缺省按中性处理）
- 内部模块：shared/events（DANMAKU_REACTED）、shared/event_bus（可选）
- 预先配置：无

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| ValueError | text 为空 | 调用方校验输入 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| init | 是 | 初始化模板、冷却、速率滑动窗口 |
| start | 是 | 订阅 danmaku:received 自动反应 |
| stop | 是 | 退订事件 |
| health | 是 | 返回冷却与速率状态 |

## 8. 领域状态说明
- 状态项：_next_available（各情感冷却）、_reply_timestamps（速率窗口）、_history
- 持久化：无
- 恢复：无
"""
import logging
import random
import threading
import time
from typing import Any, Dict, List, Optional

from src.shared.events import DANMAKU_REACTED, DANMAKU_RECEIVED

logger = logging.getLogger(__name__)


class DanmakuReactor:
    """弹幕反应器 — 基于情感分类的轻量模板回复。

    特性：情感模板库、按情感冷却、全局速率窗口、回复去重、thread-safe。
    """

    DEFAULT_REACTIONS: Dict[str, List[str]] = {
        "positive": ["谢谢支持～", "好开心呀！", "你们真好～", "嘿嘿，被夸了～",
                     "谢谢你们的喜欢！", "有你们真好～", "开心开心～", "嘿嘿嘿～"],
        "negative": ["别难过啦～", "我会加油的！", "抱歉让你失望了", "我会改进的～",
                     "别生气别生气～", "让我再努力一下！", "抱抱你～"],
        "question": ["好问题～让我想想", "这个嘛...", "你说的有道理", "嗯，怎么说呢",
                     "让我思考一下～", "好问题！", "可以可以～"],
        "neutral": ["嗯嗯～", "收到啦", "我看到了～", "嗯哼～",
                    "了解了解～", "好的好的", "在听在听～"],
    }
    DEFAULT_COOLDOWNS: Dict[str, float] = {
        "positive": 3.0, "negative": 8.0, "question": 2.0, "neutral": 5.0,
    }

    def __init__(self, event_bus=None, sentiment_analyzer=None,
                 global_cooldown: float = 1.0, rate_max: int = 20):
        self.event_bus = event_bus
        self.sentiment_analyzer = sentiment_analyzer
        self._lock = threading.RLock()
        self._reactions: Dict[str, List[str]] = {
            k: list(v) for k, v in self.DEFAULT_REACTIONS.items()}
        self._cooldowns: Dict[str, float] = dict(self.DEFAULT_COOLDOWNS)
        self._next_available: Dict[str, float] = {}
        self._global_cooldown = float(global_cooldown)
        self._global_next = 0.0
        self._rate_max = int(rate_max)
        self._rate_window = 60.0
        self._reply_timestamps: List[float] = []
        self._history: List[Dict[str, Any]] = []
        self._history_limit = 200
        self._last_reply = ""
        self._subscribed = False
        logger.info("[DanmakuReactor] 初始化完成")

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._subscribed or self.event_bus is None:
            return
        try:
            self.event_bus.subscribe(DANMAKU_RECEIVED, self._on_danmaku_received, priority=60)
            self._subscribed = True
            logger.info("[DanmakuReactor] 已订阅 %s", DANMAKU_RECEIVED)
        except Exception as e:
            logger.warning("[DanmakuReactor] 订阅失败: %s", e)

    def stop(self) -> None:
        if not self._subscribed or self.event_bus is None:
            return
        try:
            self.event_bus.unsubscribe(DANMAKU_RECEIVED, self._on_danmaku_received)
            self._subscribed = False
        except Exception as e:
            logger.warning("[DanmakuReactor] 退订失败: %s", e)

    def health(self) -> Dict[str, Any]:
        return {"status": "ok", "detail": f"subscribed={self._subscribed}, {self.get_stats()}"}

    # ---------- 事件回调 ----------

    def _on_danmaku_received(self, event: str = "", content: str = "",
                             user_name: str = "", **kwargs) -> None:
        text = (content or "").strip()
        if not text:
            return
        user = user_name or "观众"
        try:
            self.react(text, user=user)
        except Exception as e:
            logger.warning("[DanmakuReactor] 处理弹幕异常: %s", e)

    # ---------- 核心操作 ----------

    def react(self, text: str, user: str = "") -> Optional[str]:
        """对一条弹幕生成回复；被限流/冷却/去重时返回 None。"""
        text = (text or "").strip()
        if not text:
            raise ValueError("danmaku text must be non-empty")
        now = time.time()
        if now < self._global_next:
            logger.debug("[DanmakuReactor] 全局冷却中，跳过")
            return None
        if not self._check_rate_limit(now):
            logger.debug("[DanmakuReactor] 速率限制，跳过")
            return None

        sentiment, score = self._analyze(text, user)
        sentiment_cd = self._cooldowns.get(sentiment, 5.0)
        with self._lock:
            next_time = self._next_available.get(sentiment, 0.0)
        if now < next_time:
            logger.debug("[DanmakuReactor] 情感 '%s' 冷却中", sentiment)
            return None

        reply = self._pick_reply(sentiment)
        if reply is None or reply == self._last_reply:
            return None

        with self._lock:
            self._next_available[sentiment] = now + sentiment_cd
            self._global_next = now + self._global_cooldown
            self._reply_timestamps.append(now)
            self._last_reply = reply
            self._history.append({
                "input": text, "user": user, "reply": reply,
                "sentiment": sentiment, "score": score, "ts": now,
            })
            if len(self._history) > self._history_limit:
                self._history = self._history[-self._history_limit:]

        logger.info("[DanmakuReactor] 回复 '%s' → '%s' (sentiment=%s)",
                    text[:20], reply, sentiment)
        if self.event_bus:
            try:
                self.event_bus.publish(DANMAKU_REACTED, input=text, reply=reply,
                                       sentiment=sentiment, user=user)
            except Exception as e:
                logger.error("[DanmakuReactor] 发布事件失败: %s", e)
        return reply

    def register_reaction(self, sentiment: str, templates: List[str]) -> None:
        """注册/覆盖某情感的回复模板（sentiment ∈ {positive,negative,question,neutral}）。"""
        with self._lock:
            self._reactions[sentiment] = list(templates)

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            returns = sum(1 for r in self._history)
            return {
                "replies": returns,
                "history": len(self._history),
                "global_cooling": max(0.0, self._global_next - time.time()),
                "rate_window_sec": self._rate_window,
                "rate_max": self._rate_max,
            }

    # ---------- 内部 ----------

    def _analyze(self, text: str, user: str) -> tuple:
        sentiment, score = "neutral", 0.0
        if self.sentiment_analyzer is not None:
            try:
                result = self.sentiment_analyzer.analyze(text, user=user)
                sentiment = "question" if result.get("is_question") else \
                    result.get("sentiment", "neutral")
                score = result.get("score", 0.0)
            except Exception as e:
                logger.warning("[DanmakuReactor] 情感分析失败: %s", e)
        else:
            # 无分析器时做轻量启发式：问句 → question
            if any(m in text for m in ("？", "?", "怎么", "如何", "为什么")):
                sentiment = "question"
                score = 0.5
        if sentiment not in self._reactions:
            sentiment = "neutral"
        return sentiment, score

    def _pick_reply(self, sentiment: str) -> Optional[str]:
        templates = self._reactions.get(sentiment)
        if not templates:
            return None
        return random.choice(templates)

    def _check_rate_limit(self, now: float) -> bool:
        with self._lock:
            self._reply_timestamps = [t for t in self._reply_timestamps
                                      if now - t < self._rate_window]
            return len(self._reply_timestamps) < self._rate_max