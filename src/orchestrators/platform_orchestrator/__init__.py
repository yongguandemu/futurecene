"""platform_orchestrator — 三平台适配器调度官（P2）

统一管理 QQ / OBS / VTS 三个外部平台适配器：集中连接、状态查询、命令分发。
平台事件经 EventBus 发布（qq:*/obs:*/vts:*）。

# 模块内容清单（8 项契约）
1. 模块身份标识：platform · __init__ · 包入口（re-export 主类）
2. 配置契约：无
3. 输入契约：无
4. 输出契约：re-export PlatformOrchestrator 主类
5. 依赖声明：platform_orchestrator 模块
6. 错误定义：无
7. 生命周期方法：无
8. 领域状态说明：无（仅包入口）
"""
from src.orchestrators.platform_orchestrator.platform_orchestrator import (
    PlatformOrchestrator,
)

__all__ = ["PlatformOrchestrator"]