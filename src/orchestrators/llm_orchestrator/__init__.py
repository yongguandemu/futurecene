"""llm_orchestrator — LLM 调度官（P1）

只做 LLM API 调用封装（流式/重试/降级/成本统计），不含人格/记忆/意图逻辑（ADR-005）。

# 模块内容清单（8 项契约）
1. 模块身份标识：llm 调度官 · __init__ · 包入口
2. 配置契约：无（包入口，不读取配置）
3. 输入契约：无（包入口，仅 re-export）
4. 输出契约：re-export LLMOrchestrator 主类
5. 依赖声明：src.orchestrators.llm_orchestrator.llm_orchestrator
6. 错误定义：无（仅导入，无业务异常）
7. 生命周期方法：无（包入口）
8. 领域状态说明：无（包入口，re-export 主类）
"""
from src.orchestrators.llm_orchestrator.llm_orchestrator import LLMOrchestrator

__all__ = ["LLMOrchestrator"]
