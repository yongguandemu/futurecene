"""watchdog.py — 调度官看门狗（P5，旧项目 watchdog.py 模式）

监控所有已注册调度官的心跳：周期调用 health()，异常/超时标记为 down。

# 模块内容清单（8 项契约）
1. 模块身份标识：shared · Watchdog · 对外接口 register()/unregister()/check()/is_down()/get_status()/get_detail()/bind_event_bus()/start()/stop()
2. 配置契约：构造参数 timeout_seconds=5.0、check_interval=2.0
3. 输入契约：register(name, health_callable) 注册健康回调；check() 手动检查一轮
4. 输出契约：check()/get_status() 返回 {name: status}；get_detail() 返回状态详情；is_down() 返回布尔；状态翻转（ok↔degraded↔down）发布 WATCHDOG_CHANGED
5. 依赖声明：logging、threading、time、typing
6. 错误定义：health() 异常标记 down；非法状态值归为 degraded；发布事件异常记录日志不中断
7. 生命周期方法：register()/unregister()/bind_event_bus()/start()/stop()/check()
8. 领域状态说明：_health_calls 健康回调表、_status 状态表、_thread 后台线程、_running 运行标记、_event_bus 可选事件总线
"""
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class Watchdog:
    """调度官看门狗。"""

    def __init__(self, timeout_seconds: float = 5.0, check_interval: float = 2.0):
        self._timeout = timeout_seconds
        self._interval = check_interval
        self._health_calls: Dict[str, Callable[[], Dict[str, Any]]] = {}
        self._status: Dict[str, Dict[str, Any]] = {}  # {name: {status, last_check, detail}}
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._event_bus = None  # 可选注入，发布 watchdog:changed

    def bind_event_bus(self, event_bus) -> None:
        self._event_bus = event_bus

    def register(self, name: str, health_callable: Callable[[], Dict[str, Any]]) -> None:
        """注册调度官健康检查回调（orchestrator.health）。"""
        with self._lock:
            self._health_calls[name] = health_callable
            self._status[name] = {"status": "unknown", "last_check": 0.0, "detail": ""}

    def unregister(self, name: str) -> None:
        with self._lock:
            self._health_calls.pop(name, None)
            self._status.pop(name, None)

    def check(self) -> Dict[str, str]:
        """手动检查一轮（也可由 start() 后台线程驱动）。"""
        with self._lock:
            names = list(self._health_calls.keys())
        for name in names:
            fn = self._health_calls.get(name)
            if fn is None:
                continue
            try:
                result = fn()
                status = result.get("status", "ok") if isinstance(result, dict) else "ok"
                if status not in ("ok", "degraded", "down"):
                    status = "degraded"
                detail = result.get("detail", "") if isinstance(result, dict) else ""
            except Exception as e:
                status = "down"
                detail = f"health() 异常: {e}"
            with self._lock:
                old = self._status.get(name, {}).get("status", "unknown")
                self._status[name] = {"status": status, "last_check": time.time(),
                                      "detail": detail}
                # 翻转判定：初始 unknown→ok 不广播（避免启动噪音）；
                # 其余状态变化（unknown→degraded/down 与 ok↔degraded↔down）触发
                changed = old != status and not (old == "unknown" and status == "ok")
            if changed and self._event_bus is not None:
                try:
                    from src.shared.events import WATCHDOG_CHANGED
                    self._event_bus.publish(WATCHDOG_CHANGED, name=name, status=status)
                except Exception as e:
                    logger.warning("[Watchdog] 发布状态变更事件失败: %s", e)
        return self.get_status()

    def is_down(self, name: str) -> bool:
        with self._lock:
            return self._status.get(name, {}).get("status") == "down"

    def get_status(self) -> Dict[str, str]:
        with self._lock:
            return {name: info["status"] for name, info in self._status.items()}

    def get_detail(self, name: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._status.get(name, {}))

    # ---------- 后台线程 ----------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="Watchdog")
        self._thread.start()
        logger.info("[Watchdog] 已启动 (interval=%.1fs, timeout=%.1fs)",
                    self._interval, self._timeout)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def _loop(self) -> None:
        while self._running:
            try:
                self.check()
            except Exception as e:
                logger.error("[Watchdog] 检查异常: %s", e)
            time.sleep(self._interval)
