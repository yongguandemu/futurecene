"""game_orchestrator — 游戏实况调度官（P3）

VN 陪看（三套旧实现合并）+ MC 实况桥；解说发布 game:commentary_requested 由指挥官编排。

# 模块内容清单（8 项契约）
1. 模块身份标识：game 调度官 · 包入口 · re-export 主类 GameOrchestrator
2. 配置契约：无（见主模块）
3. 输入契约：无（见主模块）
4. 输出契约：re-export GameOrchestrator（见主模块）
5. 依赖声明：game_orchestrator.game_orchestrator
6. 错误定义：无（见主模块）
7. 生命周期方法：无（见主模块）
8. 领域状态说明：无（见主模块）
"""
from src.orchestrators.game_orchestrator.game_orchestrator import GameOrchestrator

__all__ = ["GameOrchestrator"]
