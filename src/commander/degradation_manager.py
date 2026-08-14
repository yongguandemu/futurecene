"""degradation_manager.py — 系统降级管理（P5，旧项目 degradation_manager.py 模式）

系统压力（成本熔断 / 看门狗检测）时自动关闭非核心调度官，恢复后还原。
核心调度官（llm/tts/bilibili/memory/safety）不可降级。

# 模块内容清单（8 项契约）
1. 模块身份标识：commander · DegradationManager · 对外 degrade/restore/snapshot
2. 配置契约：CORE_ORCHESTRATORS 核心不可降级；DEGRADABLE_ORCHESTRATORS 可降级；构造参数 degradable 可覆盖
3. 输入契约：degrade(reason) 触发降级
4. 输出契约：degrade/restore 返回操作数量；snapshot 返回降级状态与可降级列表
5. 依赖声明：logging、typing
6. 错误定义：核心调度官不降级（常量约束）；重复降级/恢复返回 0
7. 生命周期方法：degrade()/restore()/snapshot()
8. 领域状态说明：_degraded 标记、_saved 降级前开关状态（恢复用）
"""
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

CORE_ORCHESTRATORS = {"llm", "tts", "bilibili", "memory", "safety"}
DEGRADABLE_ORCHESTRATORS = {"game", "screen", "live2d"}


class DegradationManager:
    """系统降级管理。"""

    def __init__(self, switch_manager, degradable: Optional[List[str]] = None):
        self._switch_manager = switch_manager
        self._degradable = list(degradable or DEGRADABLE_ORCHESTRATORS)
        self._degraded = False
        self._saved: Dict[str, bool] = {}  # 降级前状态，恢复用

    @property
    def degraded(self) -> bool:
        return self._degraded

    def degrade(self, reason: str = "") -> int:
        """关闭全部已注册的可降级调度官；返回关闭数量。"""
        if self._degraded:
            return 0
        count = 0
        registered = set(self._switch_manager.names())
        for name in self._degradable:
            if name not in registered:
                continue
            self._saved[name] = self._switch_manager.is_enabled(name)
            if self._saved[name]:
                self._switch_manager.set_manual(name, False)
                count += 1
        self._degraded = True
        logger.warning("[DegradationManager] 已降级 %d 个调度官 (%s)", count, reason)
        return count

    def restore(self) -> int:
        """恢复降级前状态；返回恢复数量。"""
        if not self._degraded:
            return 0
        count = 0
        for name, enabled in self._saved.items():
            self._switch_manager.set_manual(name, enabled)
            if enabled:
                count += 1
        self._saved.clear()
        self._degraded = False
        logger.info("[DegradationManager] 已恢复 %d 个调度官", count)
        return count

    def snapshot(self) -> Dict:
        return {"degraded": self._degraded, "degradable": list(self._degradable)}
