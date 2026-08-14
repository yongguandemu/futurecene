"""orchestrator_registry.py — 分 brain 注册表（规格书 4.2）

原则：注册即生效。注册时自动完成三件事：
1. 将分 brain 加入可路由列表（供 Command Router 动态查询）
2. 自动创建对应开关项（供 Switch Manager 查询/控制）
3. 按声明顺序调用 start() 启动（依赖声明顺序）

# 模块内容清单（8 项契约）
1. 模块身份标识：commander · OrchestratorRegistry · 对外 register/unregister/get/all/match
2. 配置契约：无独立配置；依赖 switch_manager/event_bus
3. 输入契约：register(orchestrator)（实现 OrchestratorProtocol 的分 brain）
4. 输出契约：match(capability) 返回匹配调度官或 None；all() 返回全部实例列表
5. 依赖声明：typing、protocols.OrchestratorProtocol
6. 错误定义：DuplicateOrchestrator 同名重复注册异常
7. 生命周期方法：register()/unregister()（内部调用调度官 start()/stop()）
8. 领域状态说明：_orchestrators 名称→实例映射
"""
from typing import Dict, List, Optional

from src.commander.protocols import OrchestratorProtocol


class DuplicateOrchestrator(Exception):
    """同名分 brain 重复注册。"""


class OrchestratorRegistry:
    def __init__(self, switch_manager, event_bus):
        self._orchestrators: Dict[str, OrchestratorProtocol] = {}
        self._switch_manager = switch_manager
        self._event_bus = event_bus

    def register(self, orchestrator) -> None:
        """注册分 brain。注册即生成开关、即可路由。"""
        if orchestrator.name in self._orchestrators:
            raise DuplicateOrchestrator(orchestrator.name)
        self._orchestrators[orchestrator.name] = orchestrator
        self._switch_manager.auto_register(orchestrator.name)  # 自动生成开关项
        orchestrator.start()  # 启动（注入 EventBus 后）

    def unregister(self, name: str) -> None:
        self._orchestrators[name].stop()
        del self._orchestrators[name]
        self._switch_manager.auto_unregister(name)

    def get(self, name: str) -> Optional[OrchestratorProtocol]:
        return self._orchestrators.get(name)

    def all(self) -> List[OrchestratorProtocol]:
        return list(self._orchestrators.values())

    def match(self, capability: str) -> Optional[OrchestratorProtocol]:
        """按能力名匹配调度官（Command Router 使用）。"""
        for orch in self._orchestrators.values():
            if capability in orch.capabilities():
                return orch
        return None
