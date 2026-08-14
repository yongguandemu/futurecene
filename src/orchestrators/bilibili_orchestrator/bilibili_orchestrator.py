"""bilibili_orchestrator.py — B站调度官主类（规格书 5.4）

能力：bilibili:connect / disconnect / send_message / get_stream_code。
归一化职责：WS 消息经 normalizer 归一化为 AudienceEvent 并发布统一事件
（danmaku:received 等），调度官不做弹幕内容处理（规格书 635 行）。

# 模块内容清单（8 项契约）
1. 模块身份标识：bilibili 调度官 · bilibili_orchestrator · 能力 bilibili:connect/disconnect/send_message/get_stream_code
2. 配置契约：bilibili 域（access_key_id/access_key_secret/app_id/room_id/anchor_id），回退 os.environ（BILIBILI_ACCESS_KEY_ID 等）
3. 输入契约：handle(command) — capability + payload（room_id/text 等）
4. 输出契约：返回 {"ok","data","error"}；发布 BILIBILI_CONNECTED / BILIBILI_DISCONNECTED；WS 消息经 normalizer 发布 danmaku:received 等统一事件
5. 依赖声明：logging/os/typing；normalizer、registry、BilibiliConnector；src.shared.events
6. 错误定义：连接失败/未启动/缺 text → {"ok": False, "error": ...}；未知 capability 返回错误
7. 生命周期方法：start()/stop()/health()/handle()；connector 在 start() 时自建
8. 领域状态说明：_started 标志、_connector（WS 连接）、_event_bus、_config
"""
import logging
import os
from typing import Any, Dict, List, Optional

from src.orchestrators.bilibili_orchestrator import normalizer
from src.orchestrators.bilibili_orchestrator import registry
from src.orchestrators.bilibili_orchestrator.connector import BilibiliConnector
from src.shared.events import BILIBILI_CONNECTED, BILIBILI_DISCONNECTED

logger = logging.getLogger(__name__)


class BilibiliOrchestrator:
    """B站调度官。"""

    name = "bilibili"

    def __init__(self, event_bus, config: Optional[Dict[str, Any]] = None,
                 connector: Optional[BilibiliConnector] = None, config_loader=None):
        self._event_bus = event_bus
        self._config = config or {}
        self._connector = connector  # 测试注入；start() 时未注入则自建
        self._config_loader = config_loader  # ConfigLoader（B站密钥从 config.yaml 占位符解析）
        self._started = False
        registry.bind(self.handle)

    def capabilities(self) -> List[str]:
        return registry.capabilities()  # 从 registry 派生

    def start(self) -> None:
        if self._started:
            return
        if self._connector is None:
            # 配置契约：优先 ConfigLoader（bilibili 域），回退 os.environ
            bili_cfg = self._config_loader.get("bilibili", {}) if self._config_loader else {}
            access_key = (self._config.get("access_key")
                          or bili_cfg.get("access_key_id")
                          or os.environ.get("BILIBILI_ACCESS_KEY_ID", ""))
            access_secret = (self._config.get("access_secret")
                             or bili_cfg.get("access_key_secret")
                             or os.environ.get("BILIBILI_ACCESS_KEY_SECRET", ""))
            self._connector = BilibiliConnector(
                access_key=access_key,
                access_secret=access_secret,
                app_id=self._config.get("app_id") or os.environ.get("BILIBILI_APP_ID", ""),
                room_id=self._config.get("room_id") or os.environ.get("BILIBILI_ROOM_ID", ""),
                anchor_id=self._config.get("anchor_id", ""),
                on_message=self._on_message,
            )
        self._started = True
        logger.info("[BilibiliOrchestrator] 已启动")

    async def handle(self, command: Dict[str, Any]) -> Dict[str, Any]:
        capability = command.get("capability", "")
        payload = command.get("payload", {})
        if capability == "bilibili:connect":
            return await self._connect(payload)
        if capability == "bilibili:disconnect":
            return await self._disconnect()
        if capability == "bilibili:send_message":
            return self._send_message(payload)
        if capability == "bilibili:get_stream_code":
            return self._get_stream_code()
        return {"ok": False, "data": {}, "error": f"unknown capability: {capability}"}

    def health(self) -> Dict[str, Any]:
        if not self._started:
            return {"status": "down", "detail": "not started"}
        connected = bool(self._connector and self._connector.connected)
        return {"status": "ok" if connected else "degraded",
                "detail": "ws connected" if connected else "ws disconnected"}

    def stop(self) -> None:
        # 断开连接走 async 的 handle(bilibili:disconnect)；此处仅置标志
        self._started = False

    # ---------- 内部实现 ----------

    async def _connect(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self._connector:
            return {"ok": False, "data": {}, "error": "not started"}
        try:
            await self._connector.connect(room_id=payload.get("room_id", ""))
            self._event_bus.publish(BILIBILI_CONNECTED, room_id=payload.get("room_id", ""))
            return {"ok": True, "data": {"connected": True}, "error": None}
        except Exception as e:
            logger.error("[BilibiliOrchestrator] 连接失败: %s", e)
            return {"ok": False, "data": {}, "error": str(e)}

    async def _disconnect(self) -> Dict[str, Any]:
        if self._connector:
            await self._connector.disconnect()
        self._event_bus.publish(BILIBILI_DISCONNECTED)
        return {"ok": True, "data": {"disconnected": True}, "error": None}

    def _send_message(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self._connector:
            return {"ok": False, "data": {}, "error": "not started"}
        text = payload.get("text", "")
        if not text:
            return {"ok": False, "data": {}, "error": "text required"}
        return {"ok": True, "data": self._connector.send_danmaku(text), "error": None}

    def _get_stream_code(self) -> Dict[str, Any]:
        if not self._connector:
            return {"ok": False, "data": {}, "error": "not started"}
        return {"ok": True, "data": self._connector.get_stream_code(), "error": None}

    def _on_message(self, raw: Dict[str, Any]) -> None:
        """WS 消息回调：归一化 → 发布统一事件。"""
        event = normalizer.normalize(raw)
        if event is None:
            logger.debug("[BilibiliOrchestrator] 忽略未知消息: %s", raw.get("cmd"))
            return
        normalizer.publish(self._event_bus, event)
