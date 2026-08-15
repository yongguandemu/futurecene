"""registry.py — 游戏实况调度官内部能力注册表（规格书 5.2）

能力条目（单实现，全部由 orchestrator.handle 分发）：
- game:vn_start / game:vn_stop / game:vn_state
- game:mc_start / game:mc_stop / game:commentary
- game:op_start / game:op_stop / game:op_state / game:op_plan / game:op_command
  （通用游戏操作：使能启停 / 状态查询 / 指令规划 / 单条指令执行）
实际执行经 bind(orchestrator.handle) 绑定，resolve 返回已绑定的真实调度函数。

# 模块内容清单（8 项契约）
1. 模块身份标识：game 调度官 · registry · 能力注册表（game:vn_*/mc_*/commentary/op_* 共 11 项）
2. 配置契约：无（能力表 HANDLERS 为静态定义）
3. 输入契约：capabilities()/resolve(capability, engine)/bind(dispatch)/has(capability)
4. 输出契约：capabilities 返回 List[str]；resolve 返回已绑定调度函数；bind 无返回；has 返回 bool
5. 依赖声明：typing；src.shared.capability_registry（CapabilityRegistry/UnknownEngine）
6. 错误定义：resolve 未绑定/未知能力 → UnknownEngine（由 CapabilityRegistry 抛出）
7. 生命周期方法：bind() 在 orchestrator 构造时调用（运行时绑定）
8. 领域状态说明：_repo（CapabilityRegistry 实例）持有能力→处理器的绑定
"""
from typing import List, Optional

from src.shared.capability_registry import CapabilityRegistry, UnknownEngine

HANDLERS = {
    "game:vn_start": [object],         # 实际经 orchestrator.handle 分发
    "game:vn_stop": [object],
    "game:vn_state": [object],
    "game:mc_start": [object],
    "game:mc_stop": [object],
    "game:commentary": [object],
    "game:op_start": [object],         # 通用游戏操作：开启 AI 自动操作
    "game:op_stop": [object],          # 通用游戏操作：关闭 AI 自动操作
    "game:op_state": [object],         # 通用游戏操作：操作循环状态
    "game:op_plan": [object],          # 通用游戏操作：指令→操作计划
    "game:op_command": [object],       # 通用游戏操作：单条指令执行
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
