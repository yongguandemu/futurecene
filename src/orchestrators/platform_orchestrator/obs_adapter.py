"""obs_adapter.py — OBS 适配器（三平台适配器域）

通过 OBS WebSocket 5.x（simpleobsws）连接 OBS Studio，完成场景切换、推流控制、
源管理、截图。simpleobsws 缺失时降级为模拟模式。

事件输出（event_bus 存在时）：obs:connected / obs:disconnected /
obs:stream_started / obs:stream_stopped / obs:scene_changed。

# 模块内容清单 — obs_adapter

## 1. 模块身份标识
- 所属调度官：platform
- 能力名：adapter:obs_connect / adapter:obs_stream / adapter:obs_scene / adapter:obs_source / adapter:obs_screenshot

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| host | 否 | "127.0.0.1" | str | OBS WebSocket 地址 |
| port | 否 | 4455 | int，1-65535 | OBS WebSocket 端口 |
| password | 否 | "" | str | OBS WebSocket 密码（未启用鉴权可空） |

## 3. 输入契约
- 输入格式：`connect()` / `start_streaming(server, key)` / `stop_streaming()` / `switch_scene(scene_name)` / `get_scenes()` / `list_sources(scene_name)` / `get_screenshot(width, height)`
- server：str，RTMP 推流地址；key：str，推流码
- scene_name：str，OBS 场景名
- width/height：int，截图尺寸（默认 800x450）

## 4. 输出契约
- 成功：`connect()/start_streaming()/stop_streaming()/switch_scene()` 返回 `True`；`get_scenes()/list_sources()` 返回 dict 列表；`get_screenshot()` 返回 `{"image": base64, "format": "jpg", "scene": str, "width": int, "height": int}`；`get_status()` 返回 dict
- 失败：控制类返回 `False`；查询类返回 `[]` 或 `None`
- 事件：发布 `obs:connected / obs:disconnected / obs:stream_started / obs:stream_stopped / obs:scene_changed`

## 5. 依赖声明
- 外部服务：OBS Studio（WebSocket 服务，未运行自动降级模拟模式）
- 内部模块：`simpleobsws` 库（缺失降级模拟模式）、`src/shared/events`、event_bus（可选）
- 预先配置：OBS 需开启 WebSocket 服务并设置端口/密码

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| simpleobsws 缺失 | 未安装 simpleobsws | 降级模拟模式，不阻断链路 |
| OBS 未运行 | 连接失败/超时 | 降级模拟模式，返回模拟响应 |
| 认证超时 | wait_until_identified 失败 | 连接失败，降级模拟模式 |
| 请求超时 | 单次 OBS 请求超时 | 返回 False/None，记录错误 |
| 推流地址设置失败 | SetStreamServiceSettings 失败 | 返回 False，不开始推流 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| connect | 是 | 建立 WS 连接 + 鉴权；失败自动降级模拟 |
| disconnect | 是 | 断开 WS 连接并清理事件循环线程 |

## 8. 领域状态说明
- 状态项：`_connected`（连接标记）、`_streaming`（推流标记）、`_mock`（模拟模式标记）、`_ws`（WS 客户端）、`_loop/_loop_thread`（异步事件循环）
- 持久化：无
- 恢复：connect 时重建连接；断线后由调用方重新 connect
"""
import os
import time
import json
import logging
import threading
import asyncio
from typing import Optional, Dict, Any, List

from src.shared.events import (OBS_CONNECTED, OBS_DISCONNECTED,
                               OBS_STREAM_STARTED, OBS_STREAM_STOPPED,
                               OBS_SCENE_CHANGED)

logger = logging.getLogger(__name__)

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 4455
_DEFAULT_PASSWORD = ""


class OBSAdapter:
    """OBS Studio WebSocket 控制器。"""

    def __init__(self, event_bus=None, host: str = None, port: int = None,
                 password: str = None):
        self.event_bus = event_bus
        self._host = host or _DEFAULT_HOST
        self._port = int(port if port is not None else _DEFAULT_PORT)
        self._password = password if password is not None else _DEFAULT_PASSWORD
        self._ws = None
        self._connected = False
        self._mock = False
        self._streaming = False
        self._lock = threading.Lock()
        self._loop = None
        self._loop_thread = None
        logger.info("[OBSAdapter] 初始化完成 (host=%s, port=%d)",
                    self._host, self._port)

    # ================================================================
    # 连接管理
    # ================================================================

    def connect(self) -> bool:
        with self._lock:
            if self._connected:
                return True
            try:
                import simpleobsws
            except ImportError:
                logger.warning("[OBSAdapter] simpleobsws 未安装，降级为模拟模式")
                self._connected = True
                self._mock = True
                self._publish_event(OBS_CONNECTED, host=self._host, port=self._port)
                return True
            try:
                self._loop = asyncio.new_event_loop()
                self._loop_thread = threading.Thread(target=self._run_loop,
                                                     daemon=True)
                self._loop_thread.start()
                success = asyncio.run_coroutine_threadsafe(
                    self._async_connect(), self._loop).result(timeout=10)
                if success:
                    self._connected = True
                    self._mock = False
                    logger.info("[OBSAdapter] 已连接 (ws://%s:%d)",
                                self._host, self._port)
                    self._publish_event(OBS_CONNECTED, host=self._host,
                                        port=self._port)
                    return True
                # OBS 未运行/不可达 → 降级为模拟模式（返回模拟响应，不阻断链路）
                logger.warning("[OBSAdapter] 连接 OBS 失败，降级为模拟模式")
                self._cleanup_loop()
                self._connected = True
                self._mock = True
                self._publish_event(OBS_CONNECTED, host=self._host, port=self._port)
                return True
            except Exception as e:
                logger.warning("[OBSAdapter] 连接异常 (%s)，降级为模拟模式", e)
                self._cleanup_loop()
                self._connected = True
                self._mock = True
                self._publish_event(OBS_CONNECTED, host=self._host, port=self._port)
                return True

    def disconnect(self):
        with self._lock:
            if not self._connected:
                return
            if self._ws and self._loop:
                try:
                    asyncio.run_coroutine_threadsafe(
                        self._async_disconnect(), self._loop).result(timeout=5)
                except Exception as e:
                    logger.warning("[OBSAdapter] 断开连接异常: %s", e)
            self._connected = False
            self._streaming = False
            self._mock = False
            self._cleanup_loop()
            logger.info("[OBSAdapter] 已断开 OBS 连接")
            self._publish_event(OBS_DISCONNECTED)

    async def _async_connect(self) -> bool:
        import simpleobsws
        url = f"ws://{self._host}:{self._port}"
        self._ws = simpleobsws.WebSocketClient(
            url=url, password=self._password if self._password else None)
        await self._ws.connect()
        awaited = await self._ws.wait_until_identified(8)
        if not awaited:
            logger.error("[OBSAdapter] OBS WebSocket 认证超时")
            return False
        await self._refresh_streaming_status()
        return True

    async def _async_disconnect(self):
        if self._ws:
            await self._ws.disconnect()
            self._ws = None

    async def _refresh_streaming_status(self):
        try:
            import simpleobsws
            resp = await self._ws.call(simpleobsws.Request("GetStreamStatus"))
            if resp.ok():
                self._streaming = resp.responseData.get("outputActive", False)
        except Exception:
            logger.debug("[OBSAdapter] 获取推流状态失败")

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _cleanup_loop(self):
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop = None
        self._loop_thread = None

    # ================================================================
    # 推流控制
    # ================================================================

    def set_stream_settings(self, server: str, key: str) -> bool:
        import simpleobsws
        if not self._ensure_connected():
            return False
        try:
            params = {"streamServiceType": "rtmp_custom",
                      "streamServiceSettings": {"server": server, "key": key}}
            resp = asyncio.run_coroutine_threadsafe(
                self._ws.call(simpleobsws.Request("SetStreamServiceSettings", params)),
                self._loop).result(timeout=10)
            return resp.ok()
        except Exception as e:
            logger.error("[OBSAdapter] 设置推流异常: %s", e)
            return False

    def start_streaming(self, server: str = None, key: str = None) -> bool:
        import simpleobsws
        if not self._ensure_connected():
            return False
        if self._mock:
            self._streaming = True
            logger.info("[OBSAdapter] 模拟模式：推流已开始")
            self._publish_event(OBS_STREAM_STARTED)
            return True
        if server and key:
            if not self.set_stream_settings(server, key):
                logger.error("[OBSAdapter] 推流地址设置失败")
                return False
        try:
            resp = asyncio.run_coroutine_threadsafe(
                self._ws.call(simpleobsws.Request("StartStream")),
                self._loop).result(timeout=10)
            if resp.ok():
                self._streaming = True
                logger.info("[OBSAdapter] 推流已开始")
                self._publish_event(OBS_STREAM_STARTED)
                return True
            logger.error("[OBSAdapter] 开始推流失败: %s", resp.responseData)
            return False
        except Exception as e:
            logger.error("[OBSAdapter] 开始推流异常: %s", e)
            return False

    def stop_streaming(self) -> bool:
        import simpleobsws
        if not self._ensure_connected():
            return False
        if self._mock:
            self._streaming = False
            logger.info("[OBSAdapter] 模拟模式：推流已停止")
            self._publish_event(OBS_STREAM_STOPPED)
            return True
        try:
            resp = asyncio.run_coroutine_threadsafe(
                self._ws.call(simpleobsws.Request("StopStream")),
                self._loop).result(timeout=10)
            if resp.ok():
                self._streaming = False
                logger.info("[OBSAdapter] 推流已停止")
                self._publish_event(OBS_STREAM_STOPPED)
                return True
            logger.error("[OBSAdapter] 停止推流失败: %s", resp.responseData)
            return False
        except Exception as e:
            logger.error("[OBSAdapter] 停止推流异常: %s", e)
            return False

    # ================================================================
    # 场景管理
    # ================================================================

    def get_scenes(self) -> List[Dict[str, Any]]:
        import simpleobsws
        if not self._ensure_connected():
            return []
        if self._mock:
            return [{"name": "默认场景", "is_current": True}]  # 模拟响应
        try:
            resp = asyncio.run_coroutine_threadsafe(
                self._ws.call(simpleobsws.Request("GetSceneList")),
                self._loop).result(timeout=10)
            if resp.ok():
                data = resp.responseData
                current = data.get("currentProgramSceneName", "")
                return [{"name": s.get("sceneName", ""),
                         "is_current": s.get("sceneName", "") == current}
                        for s in data.get("scenes", [])]
            return []
        except Exception as e:
            logger.error("[OBSAdapter] 获取场景列表异常: %s", e)
            return []

    def switch_scene(self, scene_name: str) -> bool:
        import simpleobsws
        if not self._ensure_connected():
            return False
        if self._mock:
            logger.info("[OBSAdapter] 模拟模式：场景已切换: %s", scene_name)
            self._publish_event(OBS_SCENE_CHANGED, scene=scene_name)
            return True
        try:
            resp = asyncio.run_coroutine_threadsafe(
                self._ws.call(simpleobsws.Request(
                    "SetCurrentProgramScene", {"sceneName": scene_name})),
                self._loop).result(timeout=10)
            if resp.ok():
                logger.info("[OBSAdapter] 场景已切换: %s", scene_name)
                self._publish_event(OBS_SCENE_CHANGED, scene=scene_name)
                return True
            return False
        except Exception as e:
            logger.error("[OBSAdapter] 切换场景异常: %s", e)
            return False

    def list_sources(self, scene_name: str = "") -> List[Dict[str, Any]]:
        import simpleobsws
        if not self._ensure_connected():
            return []
        try:
            if not scene_name:
                resp = asyncio.run_coroutine_threadsafe(
                    self._ws.call(simpleobsws.Request("GetCurrentProgramScene")),
                    self._loop).result(timeout=10)
                if not resp.ok():
                    return []
                scene_name = resp.responseData.get("currentProgramSceneName", "")
            resp = asyncio.run_coroutine_threadsafe(
                self._ws.call(simpleobsws.Request(
                    "GetSceneItemList", {"sceneName": scene_name})),
                self._loop).result(timeout=10)
            if resp.ok():
                return [{"name": item.get("sourceName", ""),
                         "type": item.get("inputKind", "") or item.get("sourceType", ""),
                         "enabled": item.get("sceneItemEnabled", True)}
                        for item in resp.responseData.get("sceneItems", [])]
            return []
        except Exception as e:
            logger.error("[OBSAdapter] 获取源列表异常: %s", e)
            return []

    def get_screenshot(self, width: int = 800, height: int = 450):
        import simpleobsws
        if not self._ensure_connected():
            return None
        try:
            resp_scene = asyncio.run_coroutine_threadsafe(
                self._ws.call(simpleobsws.Request("GetCurrentProgramScene")),
                self._loop).result(timeout=10)
            if not resp_scene.ok():
                return None
            scene_name = resp_scene.responseData.get("currentProgramSceneName", "")
            if not scene_name:
                return None
            resp = asyncio.run_coroutine_threadsafe(
                self._ws.call(simpleobsws.Request("GetSourceScreenshot",
                    {"sourceName": scene_name, "imageFormat": "jpg",
                     "imageWidth": width, "imageHeight": height})),
                self._loop).result(timeout=15)
            if resp.ok():
                image_b64 = resp.responseData.get("imageData", "")
                if image_b64.startswith("data:"):
                    image_b64 = image_b64.split(",", 1)[-1]
                return {"image": image_b64, "format": "jpg", "scene": scene_name,
                        "width": width, "height": height}
            return None
        except Exception as e:
            logger.error("[OBSAdapter] 截图异常: %s", e)
            return None

    # ================================================================
    # 状态查询
    # ================================================================

    def is_connected(self) -> bool:
        return self._connected

    def is_streaming(self) -> bool:
        return self._streaming

    def get_status(self) -> Dict[str, Any]:
        return {"connected": self._connected, "streaming": self._streaming,
                "host": self._host, "port": self._port,
                "mock": self._mock}

    def _ensure_connected(self) -> bool:
        if self._connected:
            return True
        logger.warning("[OBSAdapter] 未连接，尝试自动连接...")
        return self.connect()

    def _publish_event(self, event_name: str, **data):
        if self.event_bus:
            try:
                self.event_bus.publish(event_name, **data)
            except Exception as e:
                logger.debug("[OBSAdapter] 发布事件失败: %s", e)