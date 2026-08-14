"""memory_orchestrator — 记忆调度官（P4）

短期（内存缓存按会话）+ 长期（SQLite 持久化）+ 关键词检索；固化由指挥官触发。

# 模块内容清单（8 项契约）
1. 模块身份标识：memory 调度官 · __init__ · 包入口
2. 配置契约：无（包入口，不读取配置）
3. 输入契约：无（包入口，仅 re-export）
4. 输出契约：re-export MemoryOrchestrator 主类
5. 依赖声明：src.orchestrators.memory_orchestrator.memory_orchestrator
6. 错误定义：无（仅导入，无业务异常）
7. 生命周期方法：无（包入口）
8. 领域状态说明：无（包入口，re-export 主类）
"""
from src.orchestrators.memory_orchestrator.memory_orchestrator import MemoryOrchestrator

__all__ = ["MemoryOrchestrator"]
