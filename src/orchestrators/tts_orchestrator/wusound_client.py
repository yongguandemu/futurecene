"""wusound_client.py — 悟声 TTS 客户端（主引擎，旧项目 core/tts_engine_wusound.py）

悟声 AI 平台 V3.0（https://v1.wusound.cn）：
- Bearer 鉴权，POST /api/tts/simple-generate
- 响应 data.audio 为音频 URL → 下载 MP3 → 转 WAV（24kHz/16bit/mono）
- emo_switch 五维情绪、preset 风格、flash 低延迟、vivid 生动、speechRate 语速

API 文档: https://dev.wusound.cn/

# 模块内容清单（8 项契约）
1. 模块身份标识：tts · WusoundClient · 能力 tts:synthesize 主引擎（悟声）
2. 配置契约：api_key、voice_id、preset、language、flash、vivid、break_clone、timeout、sample_rate
3. 输入契约：synthesize(text, voice_id, mood) 文本、可选音色与情绪
4. 输出契约：(音频字节流, 采样率, 格式 wav/mp3)
5. 依赖声明：io、logging、os、subprocess、tempfile、typing、requests（可选）、imageio_ffmpeg（可选）
6. 错误定义：requests 未安装/未配置 api_key/voice_id 抛 RuntimeError；API 非 200/业务错误/无 audio URL/空音频抛 RuntimeError；MP3→WAV 转换失败回退返回 MP3
7. 生命周期方法：无（客户端对象）
8. 领域状态说明：无（无状态；仅保存配置参数）
"""
import io
import logging
import os
import subprocess
import tempfile
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import requests
except ImportError:
    requests = None  # type: ignore

try:
    import imageio_ffmpeg
    _FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    _FFMPEG_PATH = "ffmpeg"

BASE_URL = "https://v1.wusound.cn"
SYNC_ENDPOINT = "/api/tts/simple-generate"

# 情绪 → 悟声 emo_switch 五维 [生气, 开心, 中立, 难过, 匹配上下文]（旧项目 EMO_SWITCH_MAP）
EMO_SWITCH_MAP = {
    "default": [0, 0, 0, 0, 0],
    "happy": [0, 7, 0, 0, 3],
    "calm": [0, 0, 5, 0, 3],
    "sad": [0, 0, 2, 6, 3],
    "shy": [0, 2, 3, 0, 5],
    "angry": [7, 0, 0, 0, 3],
}


class WusoundClient:
    """悟声 TTS 客户端（主引擎）。"""

    engine_name = "wusound"

    def __init__(self, api_key: str = "", voice_id: str = "", preset: str = "balance",
                 language: str = "auto", flash: bool = False, vivid: bool = False,
                 break_clone: bool = True, timeout: float = 30.0,
                 sample_rate: int = 24000):
        if requests is None:
            raise RuntimeError("requests 库未安装，请执行 pip install requests")
        self._api_key = api_key
        self._voice_id = voice_id
        self._preset = preset
        self._language = language
        self._flash = flash
        self._vivid = vivid
        self._break_clone = break_clone
        self._timeout = timeout
        self._sample_rate = sample_rate

    def synthesize(self, text: str, voice_id: Optional[str] = None,
                   mood: str = "default") -> Tuple[bytes, int, str]:
        """合成文本 → (音频字节流, 采样率, 格式)。

        返回 WAV（转换成功）或原始 MP3（转换失败回退，旧项目 _mp3_to_wav 兜底行为）。
        """
        if not self._api_key:
            raise RuntimeError("WUSOUND_API_KEY 未配置")
        vid = voice_id or self._voice_id
        if not vid:
            raise RuntimeError("wusound voice_id 未配置")

        payload = {
            "voiceId": vid,
            "text": text,
            "promptId": "default",
            "preset": self._preset,
            "break_clone": self._break_clone,
            "language": self._language,
            "vivid": self._vivid,
            "emo_switch": EMO_SWITCH_MAP.get(mood, EMO_SWITCH_MAP["default"]),
            "speechRate": 1.0,
            "flash": self._flash,
            "stream": False,
            "seed": -1,
            "srt": False,
        }
        headers = {"Authorization": f"Bearer {self._api_key}",
                   "Content-Type": "application/json"}

        resp = requests.post(f"{BASE_URL}{SYNC_ENDPOINT}", headers=headers,
                             json=payload, timeout=self._timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"悟声 API {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if data.get("status") != 200:
            raise RuntimeError(f"悟声业务错误: {data.get('message', 'unknown')}")
        audio_url = data.get("data", {}).get("audio", "")
        if not audio_url:
            raise RuntimeError("悟声响应无 audio URL")

        audio = requests.get(audio_url, timeout=self._timeout).content
        if not audio:
            raise RuntimeError("悟声音频下载为空")
        wav = self._mp3_to_wav(audio)
        if wav is not None:
            return wav, self._sample_rate, "wav"
        logger.warning("[Wusound] MP3→WAV 转换失败，返回原始 MP3")
        return audio, self._sample_rate, "mp3"

    def _mp3_to_wav(self, mp3_bytes: bytes) -> Optional[bytes]:
        """MP3 → WAV（24kHz/16bit/mono），ffmpeg 转换（imageio-ffmpeg 自带二进制）。"""
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                tmp.write(mp3_bytes)
                tmp_mp3 = tmp.name
            tmp_wav = tmp_mp3.replace(".mp3", ".wav")
            result = subprocess.run(
                [_FFMPEG_PATH, "-y", "-i", tmp_mp3,
                 "-ar", str(self._sample_rate), "-ac", "1", "-sample_fmt", "s16",
                 tmp_wav],
                capture_output=True, timeout=15,
            )
            if result.returncode == 0 and os.path.exists(tmp_wav):
                wav = open(tmp_wav, "rb").read()
                os.unlink(tmp_mp3)
                os.unlink(tmp_wav)
                if wav[:4] == b"RIFF":
                    return wav
            return None
        except Exception as e:
            logger.warning("[Wusound] ffmpeg 转换异常: %s", e)
            return None
