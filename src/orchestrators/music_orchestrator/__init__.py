"""music_orchestrator — 音乐系统调度官（P2）

播放控制（ffplay 后端）+ 歌曲库/点歌队列。播放状态经事件总线发布，
供 VoiceBridge 做 TTS 互斥。

# 模块内容清单（8 项契约）
1. 模块身份标识：music 调度官 · __init__ · 包入口
2. 配置契约：无（包入口，不读取配置）
3. 输入契约：无（包入口，仅 re-export）
4. 输出契约：re-export MusicOrchestrator 主类
5. 依赖声明：src.orchestrators.music_orchestrator.music_orchestrator
6. 错误定义：无（仅导入，无业务异常）
7. 生命周期方法：无（包入口）
8. 领域状态说明：无（包入口，re-export 主类）
"""
from src.orchestrators.music_orchestrator.music_orchestrator import MusicOrchestrator

__all__ = ["MusicOrchestrator"]