"""registry.py — 屏幕控制调度官内部能力注册表（规格书 5.2）

能力条目（单实现，全部由 orchestrator.handle 分发）：
- screen:capture / screen:click / screen:keypress / screen:execute_plan
capture/input/vision 为内部工具模块，实际执行经 bind(orchestrator.handle) 绑定。

# 模块内容清单（8 项契约）
1. 模块身份标识：screen · registry · 能力注册表（screen:capture/click/keypress/execute_plan）
2. 配置契约：无
3. 输入契约：capability 名称 + engine 参数
4. 输出契约：capabilities() 清单 / resolve() 调度函数 / has() 布尔
5. 依赖声明：typing、src.shared.capability_registry（CapabilityRegistry、UnknownEngine）
6. 错误定义：resolve 未绑定时抛 UnknownEngine
7. 生命周期方法：bind(dispatch) 运行时绑定
8. 领域状态说明：_repo 能力注册表实例
"""
from typing import List, Optional

from src.shared.capability_registry import CapabilityRegistry, UnknownEngine

HANDLERS = {
    "screen:capture": [object],        # 实际由主类调用 capture.py
    "screen:click": [object],
    "screen:keypress": [object],
    "screen:execute_plan": [object],
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
