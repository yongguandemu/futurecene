"""tts_orchestrator — TTS 调度官（补齐）

DashScope CosyVoice 合成 + 缓存 + 流式分片；发布 tts:audio_ready 供表达领域协作。

# 模块内容清单（8 项契约）
1. 模块身份标识：tts · __init__ · 包入口（re-export 主类）
2. 配置契约：无
3. 输入契约：无
4. 输出契约：re-export TTSOrchestrator 主类
5. 依赖声明：tts_orchestrator 模块
6. 错误定义：无
7. 生命周期方法：无
8. 领域状态说明：无（仅包入口）
"""
from src.orchestrators.tts_orchestrator.tts_orchestrator import TTSOrchestrator

__all__ = ["TTSOrchestrator"]
