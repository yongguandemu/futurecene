"""local_player.py — 本机 TTS 音频播放（直播测试台 / 无前端浏览器场景）

订阅 tts:audio_ready → 从缓存目录取音频文件 → 本机扬声器播放（winsound，WAV）。
零外部依赖（Windows 内置 winsound）；播放放独立守护线程，不阻塞事件回调。
双角色互斥已由协作协调器保证同一时刻仅一人发声，故单声道顺序播放即可。

# 模块内容清单 — local_player

## 1. 模块身份标识
- 所属调度官：tts（TTS 合成域）
- 能力名：tts:local_playback（本机扬声器播放，经事件订阅触发）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| cache_dir | 否 | data/cache/tts | str | 音频缓存目录（audio_id 即文件名） |
| tts.local_playback（config.yaml） | 否 | false | bool | 装配开关：是否启用本机播放 |

## 3. 输入契约
- 输入格式：订阅 `tts:audio_ready`（audio_id + role）；播放 `cache_dir / audio_id`
- audio_id：str，缓存文件名（与 WS /ws/tts_audio 同解析逻辑）

## 4. 输出契约
- 成功：winsound.PlaySound 播放缓存音频（SND_FILENAME | SND_ASYNC 异步）
- 失败：文件缺失/播放异常 → warning 日志，不抛错、不影响事件链
- 格式守卫：播放前校验 WAV 头（RIFF）；MP3 等非 WAV 文件跳过并 warning
  （修复：winsound 播放失败会触发 Windows 系统提示音）
- 事件：无（纯消费者）

## 5. 依赖声明
- 外部服务：本机声卡（winsound，Windows 内置；仅支持 WAV——两引擎输出均为 WAV）
- 内部模块：shared.events（TTS_AUDIO_READY）
- 预先配置：无

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 文件不存在 | audio_id 未落盘/被清理 | warning 跳过 |
| 播放失败 | 声卡不可用/格式不支持 | warning（winsound 抛 OSError） |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start() | 是 | 订阅 tts:audio_ready（幂等） |
| stop() | 是 | 退订（幂等） |

## 8. 领域状态说明
- 状态项：_started（订阅标记）、_play_count（已播放计数，测试/观测用）
- 持久化：无
- 恢复：无
"""
import logging
import threading
import time
import winsound
from pathlib import Path
from typing import Optional

from src.orchestrators.tts_orchestrator.tts_orchestrator import DEFAULT_CACHE_DIR
from src.shared.events import TTS_AUDIO_READY

logger = logging.getLogger(__name__)


class LocalTTSSpeaker:
    """本机 TTS 播放器：订阅 tts:audio_ready，播放缓存音频到本机扬声器。"""

    def __init__(self, event_bus, cache_dir: str = ""):
        self._event_bus = event_bus
        self._cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
        self._started = False
        self._play_count = 0

    def start(self) -> None:
        if self._started:
            return
        self._event_bus.subscribe(TTS_AUDIO_READY, self._on_audio_ready)
        self._started = True
        logger.info("[LocalTTSSpeaker] 已订阅 tts:audio_ready（本机播放）")

    def stop(self) -> None:
        if self._started:
            self._event_bus.unsubscribe(TTS_AUDIO_READY, self._on_audio_ready)
            self._started = False

    def _on_audio_ready(self, event: str, audio_id: str = "", **kwargs) -> None:
        if not audio_id:
            return
        path = self._cache_dir / audio_id
        if not path.exists():
            logger.warning("[LocalTTSSpeaker] 音频文件不存在: %s", path)
            return
        if not self._is_wav(path):
            logger.warning("[LocalTTSSpeaker] 跳过非 WAV 音频（winsound 仅支持 WAV）: %s", path.name)
            return
        threading.Thread(target=self._play, args=(path,), daemon=True,
                         name="local-tts-play").start()

    @staticmethod
    def _is_wav(path: Path) -> bool:
        """校验文件为真实 WAV（RIFF 头）。修复：MP3 伪装 .wav 时 winsound
        播放失败会触发 Windows 系统提示音。"""
        try:
            with open(path, "rb") as f:
                return f.read(4) == b"RIFF"
        except OSError:
            return False

    def _play(self, path: Path) -> None:
        try:
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
            self._play_count += 1
            logger.info("[LocalTTSSpeaker] 本机播放: %s", path.name)
        except Exception as exc:
            logger.warning("[LocalTTSSpeaker] 播放失败 %s: %s", path.name, exc)

    def wait_played(self, timeout: float = 3.0) -> bool:
        """测试辅助：等待一次播放发生（轮询 _play_count）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._play_count > 0:
                return True
            time.sleep(0.02)
        return False
