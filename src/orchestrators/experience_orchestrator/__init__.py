"""experience_orchestrator — 游戏经验学习调度官（P2）

经验驱动决策（Voyager/GITM 式）：状态编码 → 经验检索 → 种子规则 → 子任务 →
LLM 探索；反馈回写经验库 + 目标规则进化。跨会话累积，游戏无关。

# 模块内容清单（8 项契约）
1. 模块身份标识：experience 调度官 · 包入口 · re-export 主类 ExperienceOrchestrator
2. 配置契约：无（见主模块）
3. 输入契约：无（见主模块）
4. 输出契约：re-export ExperienceOrchestrator（见主模块）
5. 依赖声明：experience_orchestrator.experience_orchestrator
6. 错误定义：无（见主模块）
7. 生命周期方法：无（见主模块）
8. 领域状态说明：无（见主模块）
"""
from src.orchestrators.experience_orchestrator.experience_orchestrator import (
    ExperienceOrchestrator,
)

__all__ = ["ExperienceOrchestrator"]