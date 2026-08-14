"""live2d_orchestrator.py — Live2D 调度官主类（规格书 5.4）

能力：live2d:load / expression / motion / lip_sync。
- 后端只维护状态机 + 事件（前端 PixiJS 经 WS 渲染，规格书 8.1 live2d_stream/）
- 表达领域协作（规格书 3.4/5.5）：订阅 tts:audio_ready 自动触发口型同步
- 多角色：_models 按 role 保存独立模型状态；LIVE2D_* 事件均携带 role；
  tts:audio_ready 按 role 路由口型（口型结束线程按 role 隔离）
- 发布 LIVE2D_* 事件 + FRONTEND_STATUS_UPDATE（供前端 WS 转发控制指令）

# 模块内容清单（8 项契约）
1. 模块身份标识：live2d 调度官 · live2d_orchestrator · 能力 live2d:load/expression/motion/lip_sync
2. 配置契约：无（DEFAULT_MODEL="小恶魔"、DEFAULT_ROLE="yuki"、VALID_EXPRESSIONS/VALID_MOTIONS 为常量）
3. 输入契约：handle(command) — capability + payload（model_name/expression/motion/audio_id/duration_ms/role）；订阅 tts:audio_ready 事件（payload 含 role）
4. 输出契约：返回 {"ok","data","error"}；发布 LIVE2D_LOADED/EXPRESSION_CHANGED/MOTION_TRIGGERED/LIP_SYNC_START/LIP_SYNC_END（均带 role） + FRONTEND_STATUS_UPDATE
5. 依赖声明：logging/threading/time/typing；registry；src.shared.events
6. 错误定义：模型未加载 → {"ok": False, "error": "模型未加载..."}；未知表情/动作 → 返回错误
7. 生命周期方法：start()/stop()/health()/snapshot()/handle()；start 订阅 tts:audio_ready、stop 退订
8. 领域状态说明：_models（role -> ModelState）/ _lip_threads（role -> Thread）/ _started
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
DEFAULT_ROLE = "yuki"
VALID_EXPRESSIONS = {"开心", "难过", "惊讶", "害羞", "生气", "平静"}
VALID_MOTIONS = {"wave", "nod", "shake", "idle"}


class Live2DOrchestrator:
    """Live2D 调度官（多模型状态机：role -> ModelState）。"""

    name = "live2d"

    def __init__(self, event_bus):
        self._event_bus = event_bus
        self._models: Dict[str, Dict[str, Any]] = {}   # role -> ModelState
        self._lip_threads: Dict[str, threading.Thread] = {}
        self._started = False
        registry.bind(self.handle)

    def capabilities(self) -> List[str]:
        return registry.capabilities()

    def start(self) -> None:
        if self._started:
            return
        # 表达领域协作：订阅 TTS 音频就绪 → 按 role 自动口型同步（规格书 3.4）
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
                "detail": f"models={list(self._models.keys()) or '未加载'}"}

    def stop(self) -> None:
        self._event_bus.unsubscribe(TTS_AUDIO_READY, self._on_audio_ready)
        self._started = False

    def snapshot(self) -> Dict[str, Any]:
        """兼容旧字段（取第一个角色）+ 新增 models 按角色返回。"""
        first = next(iter(self._models.values()), {})
        return {"model": first.get("model"), "expression": first.get("expression"),
                "motion": first.get("motion"), "lip_sync": dict(first.get("lip_sync", {})),
                "models": {r: dict(m) for r, m in self._models.items()}}

    # ---------- 内部实现 ----------

    def _state(self, role: str) -> Dict[str, Any]:
        """取/建角色模型状态（默认 role=DEFAULT_ROLE，向后兼容）。"""
        if role not in self._models:
            self._models[role] = {"model": None, "expression": "平静",
                                  "motion": "idle", "lip_sync": {}}
        return self._models[role]

    def _load(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        role = payload.get("role", DEFAULT_ROLE)
        model_name = payload.get("model_name", DEFAULT_MODEL)
        st = self._state(role)
        st["model"] = model_name
        self._event_bus.publish(LIVE2D_LOADED, model=model_name, role=role)
        self._push_status()
        return {"ok": True, "data": {"loaded": True, "model": model_name, "role": role},
                "error": None}

    def _expression_change(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        role = payload.get("role", DEFAULT_ROLE)
        st = self._state(role)
        if st["model"] is None:
            return {"ok": False, "data": {}, "error": "模型未加载，请先 live2d:load"}
        expression = payload.get("expression", "平静")
        if expression not in VALID_EXPRESSIONS:
            return {"ok": False, "data": {}, "error": f"未知表情: {expression}"}
        st["expression"] = expression
        self._event_bus.publish(LIVE2D_EXPRESSION_CHANGED, expression=expression, role=role)
        self._push_status()
        return {"ok": True, "data": {"applied": True}, "error": None}

    def _motion_trigger(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        role = payload.get("role", DEFAULT_ROLE)
        st = self._state(role)
        if st["model"] is None:
            return {"ok": False, "data": {}, "error": "模型未加载，请先 live2d:load"}
        motion = payload.get("motion", "idle")
        if motion not in VALID_MOTIONS:
            return {"ok": False, "data": {}, "error": f"未知动作: {motion}"}
        st["motion"] = motion
        self._event_bus.publish(LIVE2D_MOTION_TRIGGERED, motion=motion, role=role)
        self._push_status()
        return {"ok": True, "data": {"triggered": True}, "error": None}

    def _lip_sync(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._start_lip_sync(payload.get("role", DEFAULT_ROLE),
                                    payload.get("audio_id", ""),
                                    int(payload.get("duration_ms", 1500)))

    def _start_lip_sync(self, role: str, audio_id: str, duration_ms: int) -> Dict[str, Any]:
        st = self._state(role)
        if st["model"] is None:
            return {"ok": False, "data": {}, "error": "模型未加载，请先 live2d:load"}
        st["lip_sync"] = {"audio_id": audio_id, "started_at": time.time(),
                          "duration_ms": duration_ms}
        self._event_bus.publish(LIVE2D_LIP_SYNC_START, audio_id=audio_id,
                                duration_ms=duration_ms, role=role)
        self._push_status()

        # 自动结束口型（异步，按 role 隔离）
        def _end(r=role, aid=audio_id):
            time.sleep(max(duration_ms, 50) / 1000.0)
            if self._models.get(r, {}).get("lip_sync", {}).get("audio_id") == aid:
                self._models[r]["lip_sync"] = {}
                self._event_bus.publish(LIVE2D_LIP_SYNC_END, audio_id=aid, role=r)
                self._push_status()

        self._lip_threads[role] = threading.Thread(target=_end, daemon=True,
                                                   name=f"Live2D-lip-{role}")
        self._lip_threads[role].start()
        return {"ok": True, "data": {"started": True}, "error": None}

    def _on_audio_ready(self, event: str, audio_id: str, duration_ms: int = 1500,
                        role: str = DEFAULT_ROLE, **kwargs) -> None:
        """表达领域协作：role 由 tts:audio_ready 事件携带 → 按 role 路由口型
        （事件缺失 role 时默认 DEFAULT_ROLE，规格书 3.4）。"""
        self._start_lip_sync(role, audio_id, duration_ms)

    def _push_status(self) -> None:
        """推送状态给前端（WS 网关转发 FRONTEND_STATUS_UPDATE，domain 保持 live2d）。"""
        self._event_bus.publish(FRONTEND_STATUS_UPDATE, domain="live2d",
                                data=self.snapshot())
