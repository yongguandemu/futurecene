"""tts_orchestrator.py — TTS 调度官主类（规格书 5.4）

能力：tts:synthesize / stream_synthesize / stop / cache_clean。
引擎降级链（旧项目 profiles/*/tts_config.yaml）：wusound（主）→ dashscope CosyVoice（备）。
- 合成音频写 data/cache/tts/（事件只传 audio_id，不传音频字节，规格书 3.5）
- 缓存命中直接复用；发布 tts:audio_ready 供表达领域（Live2D 口型）订阅
- 职责边界（5.5）：不驱动 Live2D，只发布事件。

# 模块内容清单（8 项契约）
1. 模块身份标识：tts · TTSOrchestrator · 能力 tts:synthesize/stream_synthesize/stop/cache_clean
2. 配置契约：ConfigLoader tts 域（wusound.api_key/voice_id/preset/language/flash/vivid/break_clone/timeout、dashscope.api_key/model/voice_id），回退 os.environ
3. 输入契约：handle(command) 指令字典（capability + payload：text/role/voice/chunk_size/max_age_hours）
4. 输出契约：{ok, data:{audio_id, duration_ms, engine}, error}；发布 tts:requested / tts:audio_ready（携带 role，供 Live2D 按角色口型路由）/ tts:failed 事件
5. 依赖声明：logging、os、time、pathlib、typing、registry、DashScopeTTSClient、WusoundClient、src.shared.config_loader、src.shared.events
6. 错误定义：主引擎失败降级备引擎；全部失败发布 tts:failed 并返回 error；text 缺失返回 error
7. 生命周期方法：start()/stop()/health()
8. 领域状态说明：_primary/_fallback 引擎实例、_cache_dir 缓存目录、_voice_map 音色映射、_stopping/_started 标记
"""
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.orchestrators.tts_orchestrator import registry
from src.orchestrators.tts_orchestrator.dashscope_client import (
    DashScopeTTSClient,
    ROLE_VOICES as DASHSCOPE_ROLE_VOICES,
)
from src.orchestrators.tts_orchestrator.wusound_client import WusoundClient
from src.shared.config_loader import PROJECT_ROOT
from src.shared.events import TTS_AUDIO_READY, TTS_FAILED, TTS_REQUESTED

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = PROJECT_ROOT / "data" / "cache" / "tts"

# 悟声角色音色（旧项目 profiles/*/tts_config.yaml）
WUSOUND_ROLE_VOICES = {
    "yuki": "a8df9796-da14-49b0-8db0-c0d141d3b164",
    "lilith": "a4dbb2a9-48cd-4853-85c5-24f07b354acf",
}


class TTSOrchestrator:
    """TTS 调度官。"""

    name = "tts"

    def __init__(self, event_bus, client=None, cache_dir: str = "",
                 voice_map: Optional[Dict[str, str]] = None, config_loader=None,
                 fallback_client=None):
        self._event_bus = event_bus
        self._primary = client  # 测试注入；start() 未注入则自建 WusoundClient
        self._fallback = fallback_client  # CosyVoice 备选（测试注入）
        self._cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
        self._voice_map = voice_map or WUSOUND_ROLE_VOICES
        self._config_loader = config_loader  # ConfigLoader（密钥从 config.yaml 占位符解析）
        self._stopping = False
        self._started = False
        registry.bind(self.handle)

    def capabilities(self) -> List[str]:
        return registry.capabilities()

    def start(self) -> None:
        if self._started:
            return
        # 配置契约：优先 ConfigLoader（tts 域），回退 os.environ
        tts_cfg = self._config_loader.get("tts", {}) if self._config_loader else {}
        wu_cfg = tts_cfg.get("wusound", {}) or {}
        ds_cfg = tts_cfg.get("dashscope", {}) or {}
        if self._primary is None:
            self._primary = WusoundClient(
                api_key=wu_cfg.get("api_key") or os.environ.get("WUSOUND_API_KEY", ""),
                voice_id=wu_cfg.get("voice_id") or WUSOUND_ROLE_VOICES["yuki"],
                preset=wu_cfg.get("preset", "balance"),
                language=wu_cfg.get("language", "auto"),
                flash=bool(wu_cfg.get("flash", False)),
                vivid=bool(wu_cfg.get("vivid", False)),
                break_clone=bool(wu_cfg.get("break_clone", True)),
                timeout=float(wu_cfg.get("timeout", 30)),
            )
        if self._fallback is None:
            self._fallback = DashScopeTTSClient(
                api_key=ds_cfg.get("api_key") or os.environ.get("DASHSCOPE_API_KEY", ""),
                model=ds_cfg.get("model") or "cosyvoice-v3.5-plus",
                voice=ds_cfg.get("voice_id") or DASHSCOPE_ROLE_VOICES["yuki"],
            )
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._started = True
        logger.info("[TTSOrchestrator] 已启动：primary=wusound fallback=cosyvoice")

    async def handle(self, command: Dict[str, Any]) -> Dict[str, Any]:
        capability = command.get("capability", "")
        payload = command.get("payload", {})
        if capability == "tts:synthesize":
            return self._synthesize(payload)
        if capability == "tts:stream_synthesize":
            return self._stream_synthesize(payload)
        if capability == "tts:stop":
            return self._stop()
        if capability == "tts:cache_clean":
            return self._cache_clean(payload)
        return {"ok": False, "data": {}, "error": f"unknown capability: {capability}"}

    def health(self) -> Dict[str, Any]:
        return {"status": "ok" if self._started else "down",
                "detail": f"cache_dir={self._cache_dir}"}

    def stop(self) -> None:
        self._stopping = True
        self._started = False

    # ---------- 内部实现 ----------

    def _resolve_voice(self, role: str, voice: Optional[str]) -> str:
        return voice or self._voice_map.get(role, WUSOUND_ROLE_VOICES["yuki"])

    def _synthesize(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        text = (payload.get("text") or "").strip()
        if not text:
            return {"ok": False, "data": {}, "error": "text 必填"}
        if self._primary is None:
            return {"ok": False, "data": {}, "error": "not started"}
        role = payload.get("role", "yuki")
        voice = self._resolve_voice(role, payload.get("voice"))
        self._event_bus.publish(TTS_REQUESTED, capability="tts:synthesize", text=text)

        # 降级链：wusound（主）→ cosyvoice（备）→ 失败
        last_error = None
        for client, engine in ((self._primary, "wusound"), (self._fallback, "cosyvoice")):
            if client is None:
                continue
            try:
                audio_id, audio_path, duration_ms = self._synthesize_to_cache(client, text, voice)
                if audio_id is None:
                    continue
                if engine != "wusound":
                    logger.warning("[TTSOrchestrator] 降级: wusound 失败 → %s", engine)
                self._event_bus.publish(TTS_AUDIO_READY, audio_id=audio_id,
                                        duration_ms=duration_ms, path=str(audio_path),
                                        role=role)
                return {"ok": True,
                        "data": {"audio_id": audio_id, "duration_ms": duration_ms,
                                 "engine": engine},
                        "error": None}
            except Exception as e:
                last_error = e
                logger.error("[TTSOrchestrator] %s 合成失败: %s", engine, e)
        self._event_bus.publish(TTS_FAILED, capability="tts:synthesize",
                                text=text, error=str(last_error))
        return {"ok": False, "data": {}, "error": f"TTS 合成失败: {last_error}"}

    def _synthesize_to_cache(self, client, text: str, voice: str):
        """用指定引擎合成到缓存目录；命中缓存直接复用。

        返回 (audio_id, path, duration_ms)；失败返回 (None, None, 0)。
        """
        key = self._cache_key(text, voice)
        audio_path = self._cache_dir / key
        if audio_path.exists():
            logger.debug("[TTSOrchestrator] 缓存命中: %s", key)
            return key, audio_path, self._duration_from_file(audio_path)
        try:
            audio, sample_rate, fmt = self._client_synthesize(client, text, voice)
        except Exception as e:
            logger.error("[TTSOrchestrator] 合成失败: %s", e)
            return None, None, 0
        audio_path.write_bytes(audio)
        duration_ms = self._duration(audio, sample_rate, fmt)
        logger.info("[TTSOrchestrator] 合成完成: %s (%d bytes, %dms, %s)",
                    key, len(audio), duration_ms, fmt)
        return key, audio_path, duration_ms

    @staticmethod
    def _client_synthesize(client, text: str, voice: str):
        """适配两引擎差异：wusound 用 voice_id（返回 3 元组），dashscope 用 voice（2 元组）。"""
        if isinstance(client, DashScopeTTSClient):
            audio, sample_rate = client.synthesize(text, voice=voice)
            return audio, sample_rate, "wav"
        return client.synthesize(text, voice_id=voice)

    @staticmethod
    def _cache_key(text: str, voice: str) -> str:
        """缓存文件名（text+voice 哈希，含引擎无关的音色区分）。"""
        import hashlib
        digest = hashlib.sha256(f"{voice}:{text}".encode("utf-8")).hexdigest()[:16]
        return f"tts_{digest}.wav"

    @staticmethod
    def _duration(audio: bytes, sample_rate: int, fmt: str) -> int:
        """时长：WAV（16bit mono）精确计算；MP3 按 16KB/s 粗略估算。"""
        if fmt == "wav":
            data_len = max(len(audio) - 44, 0)
            return max(300, int(data_len / 2 / max(int(sample_rate or 24000), 1) * 1000))
        return max(300, int(len(audio) / 16))  # mp3 ≈ 16 bytes/ms

    def _duration_from_file(self, audio_path: Path) -> int:
        """缓存命中时按文件字节估算时长（采样率未知按 24k）。"""
        try:
            return self._duration(audio_path.read_bytes(), 24000, "wav")
        except OSError:
            return 500

    def _stream_synthesize(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """长文本分片合成：每片发布 tts:audio_ready。"""
        text = (payload.get("text") or "").strip()
        if not text:
            return {"ok": False, "data": {}, "error": "text 必填"}
        role = payload.get("role", "yuki")
        voice = self._resolve_voice(role, payload.get("voice"))
        chunk_size = payload.get("chunk_size", 40)
        segments = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
        audio_ids: List[str] = []
        for seg in segments:
            audio_id, audio_path, duration_ms = self._synthesize_to_cache(
                self._primary, seg, voice)
            if audio_id is None:
                break
            audio_ids.append(audio_id)
            self._event_bus.publish(TTS_AUDIO_READY, audio_id=audio_id,
                                    duration_ms=duration_ms, path=str(audio_path),
                                    role=role, segment=len(audio_ids) - 1)
        return {"ok": bool(audio_ids), "data": {"segments": len(audio_ids)},
                "error": None if audio_ids else "流式合成失败"}

    def _stop(self) -> Dict[str, Any]:
        self._stopping = True
        return {"ok": True, "data": {"stopped": True}, "error": None}

    def _cache_clean(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        max_age_hours = float(payload.get("max_age_hours", 24))
        cutoff = time.time() - max_age_hours * 3600
        cleaned = 0
        freed = 0
        for f in self._cache_dir.glob("tts_*.wav"):
            try:
                stat = f.stat()
                if stat.st_mtime < cutoff:
                    freed += stat.st_size
                    f.unlink()
                    cleaned += 1
            except OSError:
                continue
        return {"ok": True, "data": {"cleaned": cleaned, "freed_bytes": freed},
                "error": None}
