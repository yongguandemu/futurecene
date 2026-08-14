"""live2d_orchestrator.py — Live2D 调度官主类（规格书 5.4）

能力：live2d:load / expression / motion / lip_sync。
- 后端只维护状态机 + 事件（前端 PixiJS 经 WS 渲染，规格书 8.1 live2d_stream/）
- 表达领域协作（规格书 3.4/5.5）：订阅 tts:audio_ready 自动触发口型同步
- 发布 LIVE2D_* 事件 + FRONTEND_STATUS_UPDATE（供前端 WS 转发控制指令）

# 模块内容清单（8 项契约）
1. 模块身份标识：live2d 调度官 · live2d_orchestrator · 能力 live2d:load/expression/motion/lip_sync
2. 配置契约：无（DEFAULT_MODEL="小恶魔"、VALID_EXPRESSIONS/VALID_MOTIONS 为常量）
3. 输入契约：handle(command) — capability + payload（model_name/expression/motion/audio_id/duration_ms）；订阅 tts:audio_ready 事件
4. 输出契约：返回 {"ok","data","error"}；发布 LIVE2D_LOADED/EXPRESSION_CHANGED/MOTION_TRIGGERED/LIP_SYNC_START/LIP_SYNC_END + FRONTEND_STATUS_UPDATE
5. 依赖声明：logging/threading/time/typing；registry；src.shared.events
6. 错误定义：模型未加载 → {"ok": False, "error": "模型未加载..."}；未知表情/动作 → 返回错误
7. 生命周期方法：start()/stop()/health()/snapshot()/handle()；start 订阅 tts:audio_ready、stop 退订
8. 领域状态说明：_model/_expression/_motion/_lip_sync/_started/_lip_thread
"""
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from src.orchestrators.live2d_orchestrator import registry
from src.shared.events import (
    FRONTEND_STATUS_UPDATE,
    LIVE2D_EXPRESSION_CHANGED,
    LIVE2D_LOADED,
    LIVE2D_LIP_SYNC_END,
    LIVE2D_LIP_SYNC_START,
    LIVE2D_MOTION_TRIGGERED,
    TTS_AUDIO_READY,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "小恶魔"
VALID_EXPRESSIONS = {"开心", "难过", "惊讶", "害羞", "生气", "平静"}
VALID_MOTIONS = {"wave", "nod", "shake", "idle"}


class Live2DOrchestrator:
    """Live2D 调度官。"""

    name = "live2d"

    def __init__(self, event_bus):
        self._event_bus = event_bus
        self._model: Optional[str] = None
        self._expression: str = "平静"
        self._motion: str = "idle"
        self._lip_sync: Dict[str, Any] = {}
        self._started = False
        self._lip_thread: Optional[threading.Thread] = None
        registry.bind(self.handle)

    def capabilities(self) -> List[str]:
        return registry.capabilities()

    def start(self) -> None:
        if self._started:
            return
        # 表达领域协作：订阅 TTS 音频就绪 → 自动口型同步（规格书 3.4）
        self._event_bus.subscribe(TTS_AUDIO_READY, self._on_audio_ready)
        self._started = True
        logger.info("[Live2DOrchestrator] 已启动（订阅 tts:audio_ready）")

    async def handle(self, command: Dict[str, Any]) -> Dict[str, Any]:
        capability = command.get("capability", "")
        payload = command.get("payload", {})
        if capability == "live2d:load":
            return self._load(payload)
        if capability == "live2d:expression":
            return self._expression_change(payload)
        if capability == "live2d:motion":
            return self._motion_trigger(payload)
        if capability == "live2d:lip_sync":
            return self._lip_sync(payload)
        return {"ok": False, "data": {}, "error": f"unknown capability: {capability}"}

    def health(self) -> Dict[str, Any]:
        return {"status": "ok" if self._started else "down",
                "detail": f"model={self._model or '未加载'}"}

    def stop(self) -> None:
        self._event_bus.unsubscribe(TTS_AUDIO_READY, self._on_audio_ready)
        self._started = False

    def snapshot(self) -> Dict[str, Any]:
        return {"model": self._model, "expression": self._expression,
                "motion": self._motion, "lip_sync": dict(self._lip_sync)}

    # ---------- 内部实现 ----------

    def _load(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        model_name = payload.get("model_name", DEFAULT_MODEL)
        self._model = model_name
        self._event_bus.publish(LIVE2D_LOADED, model=model_name)
        self._push_status()
        return {"ok": True, "data": {"loaded": True, "model": model_name}, "error": None}

    def _expression_change(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._model is None:
            return {"ok": False, "data": {}, "error": "模型未加载，请先 live2d:load"}
        expression = payload.get("expression", "平静")
        if expression not in VALID_EXPRESSIONS:
            return {"ok": False, "data": {}, "error": f"未知表情: {expression}"}
        self._expression = expression
        self._event_bus.publish(LIVE2D_EXPRESSION_CHANGED, expression=expression)
        self._push_status()
        return {"ok": True, "data": {"applied": True}, "error": None}

    def _motion_trigger(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._model is None:
            return {"ok": False, "data": {}, "error": "模型未加载，请先 live2d:load"}
        motion = payload.get("motion", "idle")
        if motion not in VALID_MOTIONS:
            return {"ok": False, "data": {}, "error": f"未知动作: {motion}"}
        self._motion = motion
        self._event_bus.publish(LIVE2D_MOTION_TRIGGERED, motion=motion)
        self._push_status()
        return {"ok": True, "data": {"triggered": True}, "error": None}

    def _lip_sync(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._start_lip_sync(payload.get("audio_id", ""),
                                    int(payload.get("duration_ms", 1500)))

    def _start_lip_sync(self, audio_id: str, duration_ms: int) -> Dict[str, Any]:
        if self._model is None:
            return {"ok": False, "data": {}, "error": "模型未加载，请先 live2d:load"}
        self._lip_sync = {"audio_id": audio_id, "started_at": time.time(),
                          "duration_ms": duration_ms}
        self._event_bus.publish(LIVE2D_LIP_SYNC_START, audio_id=audio_id,
                                duration_ms=duration_ms)
        self._push_status()
        # 自动结束口型（异步）
        def _end():
            time.sleep(max(duration_ms, 50) / 1000.0)
            if self._lip_sync.get("audio_id") == audio_id:
                self._lip_sync = {}
                self._event_bus.publish(LIVE2D_LIP_SYNC_END, audio_id=audio_id)
                self._push_status()

        self._lip_thread = threading.Thread(target=_end, daemon=True, name="Live2D-lip")
        self._lip_thread.start()
        return {"ok": True, "data": {"started": True}, "error": None}

    def _on_audio_ready(self, event: str, audio_id: str, duration_ms: int = 1500,
                        **kwargs) -> None:
        """表达领域协作：tts:audio_ready → 自动口型同步（规格书 3.4）。"""
        self._start_lip_sync(audio_id, duration_ms)

    def _push_status(self) -> None:
        """推送状态给前端（WS 网关转发 FRONTEND_STATUS_UPDATE）。"""
        self._event_bus.publish(FRONTEND_STATUS_UPDATE, domain="live2d",
                                data=self.snapshot())
