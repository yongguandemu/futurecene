"""dashscope_client.py — DashScope TTS 客户端（CosyVoice 引擎，规格书 5.4）

封装 dashscope tts_v2 SpeechSynthesizer（旧项目 core/tts_client.py 模式）：
回调收集音频字节流；默认 CosyVoice 模型 + Yuki AI 设计音色。

SDK 关键点（旧项目已验证）：SDK 1.26+ 不再接收 sample_rate 直传与 api_key 参数，
format 必须用 AudioFormat 枚举（WAV 16bit mono），api_key 走模块级 dashscope.api_key。

# 模块内容清单（8 项契约）
1. 模块身份标识：tts · DashScopeTTSClient · 能力 tts:synthesize 备引擎（CosyVoice）
2. 配置契约：api_key、model（默认 cosyvoice-v3.5-plus）、voice、timeout、sample_rate、ws_url
3. 输入契约：synthesize(text, voice) 文本与可选音色
4. 输出契约：(音频字节流, 采样率)；cache_key(text, voice) 缓存文件名
5. 依赖声明：hashlib、logging、threading、typing、dashscope.audio.tts_v2（可选）
6. 错误定义：dashscope 未安装抛 RuntimeError；合成超时抛 TimeoutError；回调错误/空音频抛 RuntimeError
7. 生命周期方法：无（客户端对象；wait() 等待合成完成）
8. 领域状态说明：_AudioCallback 收集音频字节流与错误状态
"""
import hashlib
import logging
import threading
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from dashscope.audio.tts_v2 import AudioFormat, ResultCallback, SpeechSynthesizer
except ImportError:
    AudioFormat = None  # type: ignore
    SpeechSynthesizer = None  # type: ignore

DEFAULT_MODEL = "cosyvoice-v3.5-plus"
DEFAULT_VOICE = "cosyvoice-v3.5-plus-vd-yuki-12276cf381b94a93b1ac81743b86ada2"
DEFAULT_SAMPLE_RATE = 24000
DEFAULT_WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

# 角色 → 音色映射（voice 参数可覆盖；Yuki 默认，Lilith 备用音色）
ROLE_VOICES = {
    "yuki": DEFAULT_VOICE,
    "lilith": "cosyvoice-v3.5-plus-vd-yuki-12276cf381b94a93b1ac81743b86ada2",
}

_FORMAT_TABLE = {
    8000: "WAV_8000HZ_MONO_16BIT",
    16000: "WAV_16000HZ_MONO_16BIT",
    22050: "WAV_22050HZ_MONO_16BIT",
    24000: "WAV_24000HZ_MONO_16BIT",
    44100: "WAV_44100HZ_MONO_16BIT",
    48000: "WAV_48000HZ_MONO_16BIT",
}


def _pick_audio_format(sample_rate: int = DEFAULT_SAMPLE_RATE):
    """按采样率选择 AudioFormat 枚举（旧项目 _pick_audio_format 逻辑，SDK 1.26+）。"""
    if AudioFormat is None:
        return None
    name = _FORMAT_TABLE.get(int(sample_rate), "WAV_24000HZ_MONO_16BIT")
    return getattr(AudioFormat, name)


class _AudioCallback(ResultCallback):
    """收集音频字节流。"""

    def __init__(self):
        self.audio: bytearray = bytearray()
        self._done = threading.Event()
        self.error: Optional[str] = None

    def on_open(self):
        pass

    def on_data(self, data) -> None:
        try:
            self.audio.extend(data)
        except Exception as e:
            self.error = f"on_data 失败: {e}"

    def on_complete(self):
        self._done.set()

    def on_error(self, message: str) -> None:
        self.error = message
        self._done.set()

    def on_close(self):
        self._done.set()

    def wait(self, timeout: float = 60.0) -> bool:
        return self._done.wait(timeout)


class DashScopeTTSClient:
    """DashScope TTS 客户端。"""

    engine_name = "dashscope"

    def __init__(self, api_key: str = "", model: str = DEFAULT_MODEL,
                 voice: str = DEFAULT_VOICE, timeout: float = 60.0,
                 sample_rate: int = DEFAULT_SAMPLE_RATE,
                 ws_url: str = DEFAULT_WS_URL):
        if SpeechSynthesizer is None:
            raise RuntimeError("dashscope 库未安装，请执行 pip install dashscope")
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._timeout = timeout
        self._sample_rate = sample_rate
        self._ws_url = ws_url

    def synthesize(self, text: str, voice: Optional[str] = None) -> Tuple[bytes, int]:
        """合成文本 → (音频字节流, 采样率)。

        # TODO: 确认 — 返回 WAV 16bit mono 音频；时长由主类按字节精确计算。
        """
        import dashscope
        if not dashscope.api_key:
            dashscope.api_key = self._api_key
        callback = _AudioCallback()
        fmt = _pick_audio_format(self._sample_rate)
        synthesizer = SpeechSynthesizer(
            model=self._model,
            voice=voice or self._voice,
            format=fmt,
            callback=callback,
            url=self._ws_url,
        )
        synthesizer.streaming_call(text)
        if not callback.wait(self._timeout):
            raise TimeoutError("DashScope TTS 合成超时")
        if callback.error:
            raise RuntimeError(f"DashScope TTS 失败: {callback.error}")
        if not callback.audio:
            raise RuntimeError("DashScope TTS 返回空音频")
        return bytes(callback.audio), self._sample_rate

    @staticmethod
    def cache_key(text: str, voice: str) -> str:
        """缓存文件名（text+voice 哈希）。"""
        digest = hashlib.sha256(f"{voice}:{text}".encode("utf-8")).hexdigest()[:16]
        return f"tts_{digest}.wav"
