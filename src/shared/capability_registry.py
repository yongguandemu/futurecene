"""capability_registry.py — 能力注册表辅助（指挥官-调度官架构，规格书 5.2）

各调度官内部 registry.py 复用的能力注册基座。职责：
- 声明能力名（capabilities()）
- 运行时把「能力 → 真实调度函数」绑定（bind()），消除空路由
- resolve() 返回已绑定的真实调用载体；未绑定时返回 _UNBOUND（明确占位说明）

用法（调度官 registry.py 内）：
    from src.shared.capability_registry import CapabilityRegistry, UnknownEngine
    HANDLERS = {...}
    _repo = CapabilityRegistry(HANDLERS)
    def capabilities(): return _repo.capabilities()
    def resolve(capability, engine=None): return _repo.resolve(capability, engine)
    def bind(dispatch): _repo.bind(dispatch)

# 模块内容清单（8 项契约）
1. 模块身份标识：shared · CapabilityRegistry · 对外接口 capabilities()/has()/resolve()/bind()
2. 配置契约：无外部配置（纯内存基座，不读取 config/环境变量）
3. 输入契约：bind(dispatch) 绑定真实调度函数；resolve(capability, engine=None) 能力名+可选引擎
4. 输出契约：capabilities() 返回能力名列表；resolve() 返回已绑定 dispatch 或 _UNBOUND 占位
5. 依赖声明：typing（无第三方依赖）
6. 错误定义：未知能力 resolve() 抛 KeyError；未绑定调用 _UNBOUND 抛 RuntimeError
7. 生命周期方法：bind()（运行时绑定）/resolve()（解析调用载体）
8. 领域状态说明：_handlers 能力→处理函数映射、_bound 已绑定调度函数、_UNBOUND 全局未绑定占位
"""
from typing import Any, Callable, Dict, List, Optional


class CapabilityRegistry:
    """能力注册表：能力名声明 + 运行时绑定真实调度函数。"""

    def __init__(self, handlers: Dict[str, list]):
        self._handlers = dict(handlers)
        self._bound: Optional[Callable] = None

    def bind(self, dispatch: Callable) -> None:
        """绑定真实调度函数（通常为 orchestrator.handle）。绑定后所有能力都可解析到它。"""
        self._bound = dispatch

    def capabilities(self) -> List[str]:
        return list(self._handlers.keys())

    def has(self, capability: str) -> bool:
        return capability in self._handlers

    def resolve(self, capability: str, engine: Optional[str] = None) -> Any:
        """返回能力对应的真实调用载体（已绑定的 dispatch）。

        未绑定时返回 _UNBOUND（明确占位说明，杜绝静默空路由）。
        """
        if capability not in self._handlers:
            raise KeyError(f"unknown capability: {capability}")
        if self._bound is not None:
            return self._bound
        return _UNBOUND


class UnknownEngine(Exception):
    """未注册的引擎名。"""


class _UnboundHandler:
    """未绑定占位：调用时给出明确说明，避免静默空路由。"""

    def __init__(self, capability: str = ""):
        self.capability = capability

    def __call__(self, *args, **kwargs):
        raise RuntimeError(
            f"[Registry] 能力调度函数未绑定: {self.capability or '(unknown)'}。"
            "请调用 registry.bind(orchestrator.handle) 完成绑定。")

    def __repr__(self):
        return f"<UnboundHandler: {self.capability or 'unknown'}>"


# 全局未绑定占位
_UNBOUND = _UnboundHandler()