"""registry.py — 日程调度官内部能力注册表

能力条目（单实现，全部由 orchestrator.handle 分发）：
- schedule:list   列出全部排期
- schedule:add    添加排期（cron 表达式 + 触发动作）
- schedule:remove 移除排期
- schedule:status 调度状态（启用/任务数/最近触发）

# 模块内容清单（8 项契约）
1. 模块身份标识：schedule 调度官 · registry · 能力注册表
2. 配置契约：无（HANDLERS 静态定义）
3. 输入契约：capabilities() / resolve(capability, engine) / bind(dispatch) / has(capability)
4. 输出契约：capabilities() 返回能力名列表；resolve() 返回已绑定调度函数或占位；has() 返回 bool
5. 依赖声明：src.shared.capability_registry（CapabilityRegistry / UnknownEngine）
6. 错误定义：resolve 未注册能力时经 CapabilityRegistry 抛 UnknownEngine
7. 生命周期方法：bind() 运行时绑定调度函数（orchestrator 构造时调用）
8. 领域状态说明：_repo（CapabilityRegistry 实例）
"""
from typing import List, Optional

from src.shared.capability_registry import CapabilityRegistry, UnknownEngine

HANDLERS = {
    "schedule:list": [object],
    "schedule:add": [object],
    "schedule:remove": [object],
    "schedule:status": [object],
}

_repo = CapabilityRegistry(HANDLERS)


def capabilities() -> List[str]:
    return _repo.capabilities()


def resolve(capability: str, engine: Optional[str] = None):
    return _repo.resolve(capability, engine)


def bind(dispatch):
    _repo.bind(dispatch)


def has(capability: str) -> bool:
    return _repo.has(capability)
