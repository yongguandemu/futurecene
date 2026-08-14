"""screen_control_orchestrator — 屏幕控制调度官（P3）

# 模块内容清单（8 项契约）
1. 模块身份标识：screen · __init__ · 包入口（re-export 主类）
2. 配置契约：无
3. 输入契约：无
4. 输出契约：re-export ScreenControlOrchestrator 主类
5. 依赖声明：screen_control_orchestrator 模块
6. 错误定义：无
7. 生命周期方法：无
8. 领域状态说明：无（仅包入口）
"""
from src.orchestrators.screen_control_orchestrator.screen_control_orchestrator import (
    ScreenControlOrchestrator,
)

__all__ = ["ScreenControlOrchestrator"]
