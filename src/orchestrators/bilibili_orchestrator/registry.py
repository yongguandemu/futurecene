"""registry.py — B站调度官内部能力注册表（规格书 5.2）

能力条目（单实现，全部由 connector 承载）：
- bilibili:connect / bilibili:disconnect / bilibili:send_message / bilibili:get_stream_code
实际执行经 bind(orchestrator.handle) 绑定，resolve 返回已绑定的真实调度函数。

# 模块内容清单（8 项契约）
1. 模块身份标识：bilibili 调度官 · registry · 能力注册表（bilibili:connect 等 4 项）
2. 配置契约：无（能力表 HANDLERS 为静态定义）
3. 输入契约：capabilities()/resolve(capability, engine)/bind(dispatch)/has(capability)
4. 输出契约：capabilities 返回 List[str]；resolve 返回已绑定调度函数；bind 无返回；has 返回 bool
5. 依赖声明：typing；src.shared.capability_registry（CapabilityRegistry/UnknownEngine）；connector.BilibiliConnector
6. 错误定义：resolve 未绑定/未知能力 → UnknownEngine（由 CapabilityRegistry 抛出）
7. 生命周期方法：bind() 在 orchestrator 构造时调用（运行时绑定）
8. 领域状态说明：_repo（CapabilityRegistry 实例）持有能力→处理器的绑定
"""
from typing import List, Optional

from src.orchestrators.bilibili_orchestrator.connector import BilibiliConnector
from src.shared.capability_registry import CapabilityRegistry, UnknownEngine

HANDLERS = {
    "bilibili:connect": [BilibiliConnector],
    "bilibili:disconnect": [BilibiliConnector],
    "bilibili:send_message": [BilibiliConnector],
    "bilibili:get_stream_code": [BilibiliConnector],
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
