"""registry.py — 直播间智能调度官内部能力注册表（规格书 5.2）

能力条目（单实现，全部由 orchestrator.handle 分发）：
- intel:danmaku_pool_* / intel:react* / intel:heat* / intel:speech_* /
  intel:context_* / intel:commentary_generate / intel:commentary_set_style /
  intel:pace_decide
实际执行经 bind(orchestrator.handle) 绑定，resolve 返回已绑定的真实调度函数。

# 模块内容清单（8 项契约）
1. 模块身份标识：live_intelligence 调度官 · registry · 能力注册表
2. 配置契约：无（HANDLERS 静态定义）
3. 输入契约：capabilities() / resolve(capability, engine) / bind(dispatch) / has(capability)
4. 输出契约：capabilities() 返回能力名列表；resolve() 返回已绑定调度函数或占位；has() 返回 bool
5. 依赖声明：src.shared.capability_registry（CapabilityRegistry / UnknownEngine）
6. 错误定义：resolve 未注册能力时经 CapabilityRegistry 抛 UnknownEngine
7. 生命周期方法：bind() 运行时绑定调度函数（orchestrator 构造时调用）
8. 领域状态说明：_repo（CapabilityRegistry 实例，保存 HANDLERS 与绑定分发函数）
"""
from typing import List, Optional

from src.shared.capability_registry import CapabilityRegistry, UnknownEngine

HANDLERS = {
    # 弹幕池
    "intel:danmaku_pool_add": [object],
    "intel:danmaku_pool_pending": [object],
    "intel:danmaku_pool_mark": [object],
    "intel:danmaku_pool_clear": [object],
    "intel:danmaku_pool_stats": [object],
    # 弹幕反应器
    "intel:react": [object],
    "intel:react_register": [object],
    "intel:react_stats": [object],
    # 热度追踪
    "intel:heat_record": [object],
    "intel:heat_score": [object],
    "intel:heat_level": [object],
    "intel:heat_trend": [object],
    "intel:heat_stats": [object],
    "intel:heat_history": [object],
    # 发言队列
    "intel:speech_enqueue": [object],
    "intel:speech_dequeue": [object],
    "intel:speech_peek": [object],
    "intel:speech_clear": [object],
    "intel:speech_stats": [object],
    # 情境聚合
    "intel:context_snapshot": [object],
    "intel:context_role": [object],
    "intel:context_ops": [object],
    "intel:context_stats": [object],
    # VN 解说
    "intel:commentary_generate": [object],
    "intel:commentary_set_style": [object],
    "intel:pace_decide": [object],
}

_repo = CapabilityRegistry(HANDLERS)


def capabilities() -> List[str]:
    """能力清单（主类 capabilities() 从此派生）。"""
    return _repo.capabilities()


def resolve(capability: str, engine: Optional[str] = None):
    """返回已绑定的真实调度函数（orchestrator.handle）；未绑定时返回明确占位。"""
    return _repo.resolve(capability, engine)


def bind(dispatch):
    """运行时绑定真实调度函数（orchestrator 构造时调用）。"""
    _repo.bind(dispatch)

def has(capability: str) -> bool:
    """能力名是否已注册。"""
    return _repo.has(capability)
