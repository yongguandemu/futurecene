"""platform_orchestrator.py — 三平台适配器调度官主类（规格书 P2）

能力：adapter:connect / disconnect / status / qq_send_* / obs_* / vts_*。
职责边界：统一管理 QQ / OBS / VTS 三个外部平台适配器，集中连接、状态查询与
命令分发。平台事件经 EventBus 发布（qq:*/obs:*/vts:*）。

# 模块内容清单（8 项契约）
1. 模块身份标识：platform · PlatformOrchestrator · 能力 adapter:connect/disconnect/status/qq_send_*/obs_*/vts_*
2. 配置契约：config.qq（bot_appid/secret）、config.obs（host/port/password）、config.vts（host/port/plugin_name/plugin_dev）
3. 输入契约：handle(command) 指令字典（capability + payload）
4. 输出契约：{ok, data, error} 响应字典；经 EventBus 发布 qq:*/obs:*/vts:* 事件
5. 依赖声明：logging、typing、registry、QQBotAdapter、OBSAdapter、VTSAdapter
6. 错误定义：未知 capability 返回 error；stop 时吞掉各适配器断开异常
7. 生命周期方法：start()/stop()/health()
8. 领域状态说明：_started 启动标记；持有 qq/obs/vts 三个适配器实例
"""
import logging
from typing import Any, Dict, List, Optional

from src.orchestrators.platform_orchestrator import registry
from src.orchestrators.platform_orchestrator.qq_adapter import QQBotAdapter
from src.orchestrators.platform_orchestrator.obs_adapter import OBSAdapter
from src.orchestrators.platform_orchestrator.vts_adapter import VTSAdapter

logger = logging.getLogger(__name__)


class PlatformOrchestrator:
    """三平台适配器调度官。"""

    name = "platform"

    def __init__(self, event_bus, config: Optional[Dict[str, Any]] = None,
                 qq: Optional[QQBotAdapter] = None,
                 obs: Optional[OBSAdapter] = None,
                 vts: Optional[VTSAdapter] = None):
        self._event_bus = event_bus
        cfg = config or {}
        qq_cfg = cfg.get("qq") or {}
        obs_cfg = cfg.get("obs") or {}
        vts_cfg = cfg.get("vts") or {}
        self._qq = qq or QQBotAdapter(qq_cfg, event_bus=event_bus,
                                      appid=qq_cfg.get("bot_appid", ""),
                                      secret=qq_cfg.get("secret", ""))
        self._obs = obs or OBSAdapter(event_bus=event_bus,
                                      host=obs_cfg.get("host"),
                                      port=obs_cfg.get("port"),
                                      password=obs_cfg.get("password"))
        self._vts = vts or VTSAdapter(host=vts_cfg.get("host", "127.0.0.1"),
                                      port=int(vts_cfg.get("port", 8001)),
                                      plugin_name=vts_cfg.get("plugin_name", "FutureScene"),
                                      plugin_dev=vts_cfg.get("plugin_dev", "FutureScene"),
                                      event_bus=event_bus)
        self._started = False
        registry.bind(self.handle)

    def capabilities(self) -> List[str]:
        return registry.capabilities()

    def start(self) -> None:
        self._started = True
        logger.info("[PlatformOrchestrator] 已启动")

    async def handle(self, command: Dict[str, Any]) -> Dict[str, Any]:
        capability = command.get("capability", "")
        payload = command.get("payload", {})
        if capability == "adapter:connect":
            return self._connect(payload)
        if capability == "adapter:disconnect":
            return self._disconnect(payload)
        if capability == "adapter:status":
            return self._status()
        if capability == "adapter:qq_send_group":
            return self._qq_send_group(payload)
        if capability == "adapter:qq_send_c2c":
            return self._qq_send_c2c(payload)
        if capability == "adapter:qq_send_channel":
            return self._qq_send_channel(payload)
        if capability == "adapter:obs_stream_start":
            return self._obs_stream(payload, start=True)
        if capability == "adapter:obs_stream_stop":
            return self._obs_stream(payload, start=False)
        if capability == "adapter:obs_switch_scene":
            return self._obs_scene(payload)
        if capability == "adapter:obs_scenes":
            return {"ok": True, "data": {"scenes": self._obs.get_scenes()},
                    "error": None}
        if capability == "adapter:obs_screenshot":
            return self._obs_screenshot(payload)
        if capability == "adapter:vts_param":
            return self._vts_param(payload)
        if capability == "adapter:vts_expression":
            return self._vts_expression(payload)
        if capability == "adapter:vts_hotkey":
            return self._vts_hotkey(payload)
        if capability == "adapter:vts_model":
            return {"ok": True, "data": self._vts.get_model_info() or {},
                    "error": None}
        return {"ok": False, "data": {}, "error": f"unknown capability: {capability}"}

    def health(self) -> Dict[str, Any]:
        return {"status": "ok" if self._started else "down",
                "detail": self._status()["data"]}

    def stop(self) -> None:
        try:
            self._qq.disconnect()
        except Exception:
            pass
        try:
            self._obs.disconnect()
        except Exception:
            pass
        try:
            self._vts.disconnect()
        except Exception:
            pass
        self._started = False

    # ---------- 内部实现 ----------

    def _connect(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        platform_ = payload.get("platform", "")
        if platform_ == "qq":
            return {"ok": self._qq.connect(), "data": {}, "error": None}
        if platform_ == "obs":
            return {"ok": self._obs.connect(), "data": {}, "error": None}
        if platform_ == "vts":
            return {"ok": self._vts.connect(), "data": {}, "error": None}
        # 全部连接
        self._qq.connect()
        self._obs.connect()
        self._vts.connect()
        return self._status()

    def _disconnect(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        platform_ = payload.get("platform", "")
        if platform_ == "qq":
            self._qq.disconnect()
        elif platform_ == "obs":
            self._obs.disconnect()
        elif platform_ == "vts":
            self._vts.disconnect()
        return {"ok": True, "data": {}, "error": None}

    def _status(self) -> Dict[str, Any]:
        return {"ok": True,
                "data": {"qq": self._qq.get_stats(),
                         "obs": self._obs.get_status(),
                         "vts": self._vts.get_stats()},
                "error": None}

    def _qq_send_group(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self._qq.send_group_message(payload.get("group_openid", ""),
                                             payload.get("content", ""),
                                             msg_type=payload.get("msg_type", 0),
                                             msg_id=payload.get("msg_id", ""))
        return {"ok": result["success"], "data": result, "error": None}

    def _qq_send_c2c(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self._qq.send_c2c_message(payload.get("openid", ""),
                                           payload.get("content", ""),
                                           msg_type=payload.get("msg_type", 0),
                                           msg_id=payload.get("msg_id", ""))
        return {"ok": result["success"], "data": result, "error": None}

    def _qq_send_channel(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self._qq.send_channel_message(payload.get("channel_id", ""),
                                               payload.get("content", ""),
                                               msg_id=payload.get("msg_id", ""))
        return {"ok": result["success"], "data": result, "error": None}

    def _obs_stream(self, payload: Dict[str, Any], start: bool) -> Dict[str, Any]:
        if start:
            ok = self._obs.start_streaming(payload.get("server"),
                                           payload.get("key"))
        else:
            ok = self._obs.stop_streaming()
        return {"ok": ok, "data": {"streaming": self._obs.is_streaming()},
                "error": None}

    def _obs_scene(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ok = self._obs.switch_scene(payload.get("scene", ""))
        return {"ok": ok, "data": {}, "error": None}

    def _obs_screenshot(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        shot = self._obs.get_screenshot(payload.get("width", 800),
                                        payload.get("height", 450))
        return {"ok": shot is not None, "data": shot or {}, "error": None}

    def _vts_param(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ok = self._vts.set_parameter(payload.get("param_id", ""),
                                     float(payload.get("value", 0)),
                                     weight=float(payload.get("weight", 1.0)))
        return {"ok": ok, "data": {}, "error": None}

    def _vts_expression(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ok = self._vts.set_expression(payload.get("expression", ""),
                                      intensity=float(payload.get("intensity", 1.0)))
        return {"ok": ok, "data": {}, "error": None}

    def _vts_hotkey(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ok = self._vts.trigger_hotkey(payload.get("hotkey_id", ""))
        return {"ok": ok, "data": {}, "error": None}