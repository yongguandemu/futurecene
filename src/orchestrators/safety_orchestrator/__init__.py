"""safety_orchestrator — 安全调度官（P4）

关键词规则（热重载）+ 可选模型推理（规则兜底）；只返回 verdict，策略归指挥官。

# 模块内容清单（8 项契约）
1. 模块身份标识：safety · __init__ · 包入口（re-export 主类）
2. 配置契约：无
3. 输入契约：无
4. 输出契约：re-export SafetyOrchestrator 主类
5. 依赖声明：safety_orchestrator 模块
6. 错误定义：无
7. 生命周期方法：无
8. 领域状态说明：无（仅包入口）
"""
from src.orchestrators.safety_orchestrator.safety_orchestrator import SafetyOrchestrator

__all__ = ["SafetyOrchestrator"]
