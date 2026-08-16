"""schedule_orchestrator — 日程调度官（P0 补迁：旧系统 schedule_engine/schedule_service）

排期管理：cron 表达式定时任务，到点发布 schedule:fired 事件（动作由指挥官分发执行）。
能力：schedule:list / add / remove / status。

# 模块内容清单（8 项契约）
1. 模块身份标识：schedule 调度官 · __init__ · 包入口
2. 配置契约：无（包入口，不读取配置）
3. 输入契约：无（包入口，仅 re-export）
4. 输出契约：re-export ScheduleOrchestrator 主类
5. 依赖声明：src.orchestrators.schedule_orchestrator.schedule_orchestrator
6. 错误定义：无（仅导入，无业务异常）
7. 生命周期方法：无（包入口）
8. 领域状态说明：无（包入口，re-export 主类）
"""
from src.orchestrators.schedule_orchestrator.schedule_orchestrator import ScheduleOrchestrator

__all__ = ["ScheduleOrchestrator"]
