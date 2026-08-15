"""game_operation_controller.py — 游戏操作使能与安全控制器（P3 通用游戏操作）

承接原系统 game_control.py（使能状态机）+ game_operation_brain.py（熔断/防抖/冷却）：
- GameOperationController：AI 自动操作使能状态机（enabled/source/since/stop_at）
- OperationSafety：安全护栏——熔断（连续无响应暂停）、防抖（同指令去重）、
  冷却（操作间隔，给解说/渲染留窗口）
线程安全（Lock），供 GameOperationLoop 与 game:op_start/op_stop 能力共用。

# 模块内容清单 — game_operation_controller

## 1. 模块身份标识
- 所属调度官：game
- 能力名：game:op_start / game:op_stop（承载实现）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| fuse_limit | 否 | 3 | int | 连续无响应熔断阈值 |
| fuse_pause | 否 | 60 | float 秒 | 熔断暂停时长 |
| dedup_window | 否 | 8 | float 秒 | 同指令去重窗口 |
| post_action_cooldown | 否 | 3.0 | float 秒 | 操作后冷却（给解说窗口） |

## 3. 输入契约
- 输入格式：`GameOperationController(config)`；`start(source, stop_after_seconds)/stop()/status()`
- OperationSafety：`allow(action, now)` 防抖+冷却判定；`on_result(ok, scene_changed, now)` 熔断判定

## 4. 输出契约
- 成功：start/stop 返回状态快照 dict；allow 返回 bool；on_result 返回是否熔断
- 失败：无异常路径（状态机纯内存）
- 事件：无（状态变化由调用方发布 game:op_state_changed）

## 5. 依赖声明
- 外部服务：无
- 内部模块：threading、time、logging
- 预先配置：GameOperationLoop 构造时创建

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 无（纯状态机） | - | 所有方法线程安全，不抛异常 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start/stop | 是 | 使能/禁用 AI 自动操作（stop_after_seconds>0 到时自动停） |
| status | 是 | 返回快照（到时自动停判定） |

## 8. 领域状态说明
- 状态项：_enabled/_source/_since/_stop_at/_fuse_count/_fuse_until/_last_action/_last_action_time
- 持久化：无
- 恢复：无状态持久化；start() 重建
"""
import logging
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class GameOperationController:
    """游戏操作使能状态机（线程安全）。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or {}
        self._enabled = False
        self._source = ""
        self._since = 0.0
        self._stop_at = 0.0          # 0 = 不自动停
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        with self._lock:
            self._check_auto_stop()
            return self._enabled

    def start(self, source: str = "manual",
              stop_after_seconds: Optional[float] = None) -> Dict[str, Any]:
        """开启 AI 自动操作。stop_after_seconds>0 时到时自动停。"""
        with self._lock:
            self._enabled = True
            self._source = source
            self._since = time.time()
            self._stop_at = (time.time() + stop_after_seconds) \
                if stop_after_seconds else 0.0
            logger.info("[GameOperationController] 已开启 AI 自动操作 source=%s",
                        source)
            return self._snapshot()

    def stop(self) -> Dict[str, Any]:
        """关闭 AI 自动操作（用户接管/到点）。"""
        with self._lock:
            self._enabled = False
            self._source = ""
            self._stop_at = 0.0
            logger.info("[GameOperationController] 已关闭 AI 自动操作")
            return self._snapshot()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            self._check_auto_stop()
            return self._snapshot()

    def _check_auto_stop(self) -> None:
        if self._enabled and self._stop_at and time.time() >= self._stop_at:
            self._enabled = False
            self._source = ""
            self._stop_at = 0.0
            logger.info("[GameOperationController] 到时自动停止")

    def _snapshot(self) -> Dict[str, Any]:
        return {"enabled": self._enabled, "source": self._source,
                "since": self._since, "stop_at": self._stop_at}


class OperationSafety:
    """操作安全护栏：熔断 + 防抖 + 冷却（线程安全）。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or {}
        self.fuse_limit = int(cfg.get("fuse_limit", 3))
        self.fuse_pause = float(cfg.get("fuse_pause", 60))
        self.dedup_window = float(cfg.get("dedup_window", 8))
        self.post_action_cooldown = float(cfg.get("post_action_cooldown", 3.0))
        self._lock = threading.Lock()
        self._last_action: Optional[str] = None
        self._last_action_time = 0.0
        self._fuse_count = 0
        self._fuse_until = 0.0

    def allow(self, action: str, now: Optional[float] = None) -> bool:
        """防抖 + 冷却 + 熔断判定：是否允许下发该操作。"""
        now = now or time.time()
        with self._lock:
            if now < self._fuse_until:
                return False
            if action == self._last_action and \
                    now - self._last_action_time < self.dedup_window:
                return False
            if now - self._last_action_time < self.post_action_cooldown:
                return False
            return True

    def mark_action(self, action: str, now: Optional[float] = None) -> None:
        """记录已下发操作（防抖/冷却基准）。"""
        now = now or time.time()
        with self._lock:
            self._last_action = action
            self._last_action_time = now

    def on_result(self, ok: bool, scene_changed: bool = True,
                  now: Optional[float] = None) -> bool:
        """执行结果回调：连续无变化触发熔断。返回是否已熔断。"""
        now = now or time.time()
        with self._lock:
            if ok and scene_changed:
                self._fuse_count = 0
                return False
            self._fuse_count += 1
            if self._fuse_count >= self.fuse_limit:
                self._fuse_until = now + self.fuse_pause
                logger.warning("[OperationSafety] 连续 %s 次无响应，熔断 %ss",
                               self._fuse_count, self.fuse_pause)
                self._fuse_count = 0
                return True
            return False

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"fuse_count": self._fuse_count,
                    "fuse_until": self._fuse_until,
                    "last_action": self._last_action,
                    "last_action_time": self._last_action_time}
