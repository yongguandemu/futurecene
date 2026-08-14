"""registry.py — 音乐系统调度官内部能力注册表（规格书 5.2）

能力条目（单实现，全部由 orchestrator.handle 分发）：
- music:play / pause / resume / stop / next / prev / volume / mode / state /
  stats / playlist / request_song / song_search / song_add / song_library
实际执行经 bind(orchestrator.handle) 绑定，resolve 返回已绑定的真实调度函数。

# 模块内容清单（8 项契约）
1. 模块身份标识：music 调度官 · registry · 能力注册表
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
    "music:play": [object],            # 实际经 orchestrator.handle 分发
    "music:pause": [object],
    "music:resume": [object],
    "music:stop": [object],
    "music:next": [object],
    "music:prev": [object],
    "music:volume": [object],
    "music:mode": [object],
    "music:state": [object],
    "music:stats": [object],
    "music:playlist": [object],
    "music:request_song": [object],
    "music:song_search": [object],
    "music:song_add": [object],
    "music:song_library": [object],
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
