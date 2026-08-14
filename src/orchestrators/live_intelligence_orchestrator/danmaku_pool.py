"""模块内容清单 — danmaku_pool

## 1. 模块身份标识
- 所属调度官：live_intelligence_orchestrator
- 能力名：intel:danmaku_pool_add / danmaku_pool_pending / danmaku_pool_stats / danmaku_pool_clear
- 引擎名：（单实现）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| max_size | 否 | 100 | int, 1-10000 | 弹幕池最大容量，超出清理最旧 |
| ttl | 否 | 600 | float, 秒 | 弹幕存活时长，超时视为过期 |

## 3. 输入契约
- intel:danmaku_pool_add 输入：{"text": str, "user"?: str, "platform"?: str}
  - text 必填，str，非空
  - user 可选，str，默认 ""
  - platform 可选，str ∈ {bilibili, qq, ...}，默认 "bilibili"
- danmaku_pool_pending 输入：{"limit"?: int}，limit 可选，默认 10，1-100

## 4. 输出契约
- 成功：{"ok": true, "data": {...}, "error": null}
- 失败：{"ok": false, "data": {}, "error": str}

## 5. 依赖声明
- 外部服务：无
- 内部模块：shared/events（DANMAKU_POOLED）、shared/event_bus（可选）
- 预先配置：无

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| ValueError | text 为空或类型错误 | 调用方校验输入 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| init | 是 | 读取配置、初始化锁与存储 |
| start | 否 | 订阅弹幕事件（由调度官调用） |
| stop | 否 | 退订事件 |
| health | 是 | 返回池占用与统计 |

## 8. 领域状态说明
- 状态项：_pool（弹幕列表）、_stats（计数）
- 持久化：无（纯内存，重启清空）
- 恢复：无（运行时状态，重启即重建）
"""
import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from src.shared.events import DANMAKU_POOLED, DANMAKU_RECEIVED

logger = logging.getLogger(__name__)


class DanmakuPool:
    """弹幕池 — 接收、存储、权重排序与过期淘汰弹幕。

    线程安全。弹幕对象结构：
        {"id": str, "text": str, "user": str, "platform": str,
         "timestamp": float, "weight": int, "processed": bool}
    """

    # 默认权重表（关键词命中 → 加权，提升高价值弹幕优先级）
    DEFAULT_KEYWORD_WEIGHTS = {
        "？": 3, "?": 3, "怎么": 3, "如何": 3, "为什么": 3,
        "主播": 2, "你": 2, "！": 2, "!": 2, "好": 1, "谢谢": 1,
    }
    DEFAULT_WEIGHT = 1

    def __init__(self, event_bus=None, max_size: int = 100, ttl: float = 600.0):
        self.event_bus = event_bus
        self._max_size = int(max_size)
        self._ttl = float(ttl)
        self._lock = threading.RLock()
        self._pool: List[Dict[str, Any]] = []
        self._keyword_weights: Dict[str, int] = dict(self.DEFAULT_KEYWORD_WEIGHTS)
        self._stats = {"added": 0, "pending_left": 0, "expired": 0, "cleared": 0}
        self._subscribed = False
        logger.info("[DanmakuPool] 初始化完成 (max_size=%d, ttl=%.0fs)", self._max_size, self._ttl)

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """订阅 danmaku:received 自动入池（幂等）。"""
        if self._subscribed or self.event_bus is None:
            return
        try:
            self.event_bus.subscribe(DANMAKU_RECEIVED, self._on_danmaku, priority=50)
            self._subscribed = True
            logger.info("[DanmakuPool] 已订阅 %s", DANMAKU_RECEIVED)
        except Exception as e:
            logger.warning("[DanmakuPool] 订阅失败: %s", e)

    def stop(self) -> None:
        if not self._subscribed or self.event_bus is None:
            return
        try:
            self.event_bus.unsubscribe(DANMAKU_RECEIVED, self._on_danmaku)
            self._subscribed = False
        except Exception as e:
            logger.warning("[DanmakuPool] 退订失败: %s", e)

    def health(self) -> Dict[str, Any]:
        return {"status": "ok", "detail": f"pool_size={self.size()}, {self.get_stats()}"}

    # ---------- 事件回调 ----------

    def _on_danmaku(self, event: str = "", content: str = "", user_name: str = "",
                    **kwargs) -> None:
        text = (content or "").strip()
        if not text:
            return
        self.add(text, user=user_name,
                 platform=kwargs.get("platform", kwargs.get("source", "bilibili")))

    # ---------- 核心操作 ----------

    def add(self, text: str, user: str = "", platform: str = "bilibili") -> Dict[str, Any]:
        """添加一条弹幕，返回弹幕对象。"""
        text = (text or "").strip()
        if not text:
            raise ValueError("danmaku text must be non-empty")
        danmaku = {
            "id": self._gen_id(),
            "text": text,
            "user": user or "",
            "platform": platform or "bilibili",
            "timestamp": time.time(),
            "weight": self._calc_weight(text),
            "processed": False,
        }
        with self._lock:
            self._expire_locked()
            if len(self._pool) >= self._max_size:
                self._pool.pop(0)
                self._stats["expired"] += 1
            self._pool.append(danmaku)
            self._stats["added"] += 1
        if self.event_bus:
            try:
                self.event_bus.publish(DANMAKU_POOLED, danmaku_id=danmaku["id"],
                                       text=text, user=user, platform=platform)
            except Exception as e:
                logger.warning("[DanmakuPool] 发布事件失败: %s", e)
        return dict(danmaku)

    def get_pending(self, limit: int = 10) -> List[Dict[str, Any]]:
        """返回未处理弹幕，按 weight 降序、timestamp 升序。浅拷贝。"""
        limit = max(1, min(int(limit), self._max_size))
        with self._lock:
            self._expire_locked()
            pending = [d for d in self._pool if not d["processed"]]
            pending.sort(key=lambda d: (-d["weight"], d["timestamp"]))
            return [dict(d) for d in pending[:limit]]

    def mark_processed(self, danmaku_ids: List[str]) -> int:
        """将指定弹幕标记为已处理，返回处理条数。"""
        ids = set(danmaku_ids or [])
        if not ids:
            return 0
        with self._lock:
            count = 0
            for d in self._pool:
                if d["id"] in ids and not d["processed"]:
                    d["processed"] = True
                    count += 1
            self._stats["pending_left"] = sum(1 for d in self._pool if not d["processed"])
        return count

    def clear(self) -> int:
        """清空弹幕池，返回清空条数。"""
        with self._lock:
            n = len(self._pool)
            self._pool = []
            self._stats["cleared"] += n
            self._stats["pending_left"] = 0
        return n

    def size(self) -> int:
        with self._lock:
            self._expire_locked()
            return len(self._pool)

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            self._expire_locked()
            pending = sum(1 for d in self._pool if not d["processed"])
            return {
                "size": len(self._pool),
                "pending": pending,
                "max_size": self._max_size,
                "ttl": self._ttl,
                **self._stats,
            }

    # ---------- 内部 ----------

    def _gen_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def _calc_weight(self, text: str) -> int:
        weight = self.DEFAULT_WEIGHT
        for keyword, w in self._keyword_weights.items():
            if keyword in text:
                weight += w
        return weight

    def _expire_locked(self) -> None:
        """移除超过 TTL 的弹幕（调用方须持有锁）。"""
        now = time.time()
        kept: List[Dict[str, Any]] = []
        for d in self._pool:
            if now - d["timestamp"] <= self._ttl:
                kept.append(d)
            else:
                self._stats["expired"] += 1
        self._pool = kept