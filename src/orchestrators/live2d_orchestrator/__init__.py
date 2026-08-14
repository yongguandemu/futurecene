"""live2d_orchestrator — Live2D 调度官（补齐）

状态机 + 表达领域协作（订阅 tts:audio_ready 口型同步）；前端经 WS 渲染。

# 模块内容清单（8 项契约）
1. 模块身份标识：live2d 调度官 · 包入口 · re-export 主类 Live2DOrchestrator
2. 配置契约：无（见主模块）
3. 输入契约：无（见主模块）
4. 输出契约：re-export Live2DOrchestrator（见主模块）
5. 依赖声明：live2d_orchestrator.live2d_orchestrator
6. 错误定义：无（见主模块）
7. 生命周期方法：无（见主模块）
8. 领域状态说明：无（见主模块）
"""
from src.orchestrators.live2d_orchestrator.live2d_orchestrator import Live2DOrchestrator

__all__ = ["Live2DOrchestrator"]
