"""bilibili_orchestrator — B站调度官（P2）

WS 长连接（HMAC-SHA256 签名）→ normalizer 归一化 → 发布统一观众事件；
调度官不做弹幕内容处理（规格书 635 行），消费由指挥官编排。

# 模块内容清单（8 项契约）
1. 模块身份标识：bilibili 调度官 · 包入口 · re-export 主类 BilibiliOrchestrator
2. 配置契约：无（见主模块）
3. 输入契约：无（见主模块）
4. 输出契约：re-export BilibiliOrchestrator（见主模块）
5. 依赖声明：bilibili_orchestrator.bilibili_orchestrator
6. 错误定义：无（见主模块）
7. 生命周期方法：无（见主模块）
8. 领域状态说明：无（见主模块）
"""
from src.orchestrators.bilibili_orchestrator.bilibili_orchestrator import BilibiliOrchestrator

__all__ = ["BilibiliOrchestrator"]
