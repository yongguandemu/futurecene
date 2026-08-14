"""stream_orchestrator — 无人值守直播调度官（P2）

取推流码 → ffmpeg 无头推流 → 心跳保活 → 停播，外加外部应用启动器。
状态变更经 EventBus 发布（stream:state_changed）。

# 模块内容清单（8 项契约）
1. 模块身份标识：stream · __init__ · 包入口（re-export 主类）
2. 配置契约：无
3. 输入契约：无
4. 输出契约：re-export StreamOrchestrator 主类
5. 依赖声明：stream_orchestrator 模块
6. 错误定义：无
7. 生命周期方法：无
8. 领域状态说明：无（仅包入口）
"""
from src.orchestrators.stream_orchestrator.stream_orchestrator import (
    StreamOrchestrator,
)

__all__ = ["StreamOrchestrator"]