"""vts_adapter.py — VTS 适配器（三平台适配器域）

对接 VTube Studio WebSocket API，控制 Live2D 模型参数、触发表情/热键。
websocket 库缺失时降级为模拟模式（状态机可用）。

# 模块内容清单 — vts_adapter

## 1. 模块身份标识
- 所属调度官：platform
- 能力名：adapter:vts_connect / adapter:vts_param / adapter:vts_expression / adapter:vts_hotkey

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| host | 否 | "127.0.0.1" | str | VTube Studio WS 地址 |
| port | 否 | 8001 | int，1-65535 | VTube Studio WS 端口 |
| plugin_name | 否 | "FutureScene" | str | 插件名（认证用） |
| plugin_dev | 否 | "FutureScene" | str | 插件开发者（认证用） |

## 3. 输入契约
- 输入格式：`connect()` / `set_parameter(param_id, value, weight)` / `set_parameters(params)` / `trigger_hotkey(hotkey_id)` / `set_expression(expression_name, intensity)` / `get_model_info()`
- param_id：str，VTS 参数 ID；value：float 0.0-1.0（越界钳制）；weight：float 0.0-1.0
- params：`[{"id": str, "value": float, "weight": float}, ...]`
- hotkey_id / expression_name：str，VTS 热键/表情 ID

## 4. 输出契约
- 成功：`set_parameter()/set_parameters()/trigger_hotkey()/set_expression()` 返回 `True`；`get_model_info()` 返回 `{"model_name", "model_id", "vts_folder"}`；`get_stats()` 返回 dict
- 失败：`get_model_info()` 未连接/命令失败返回 `None`
- 事件：发布 `vts:connected / vts:disconnected`

## 5. 依赖声明
- 外部服务：VTube Studio（WebSocket 服务，未运行自动降级模拟模式）
- 内部模块：`websocket` 库（缺失降级模拟模式）、`src/shared/events`、event_bus（可选）
- 预先配置：VTS 需开启 WebSocket 插件 API

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| websocket 缺失 | 未安装 websocket 库 | 降级模拟模式，不阻断链路 |
| VTS 未运行 | 连接失败/超时 | 降级模拟模式，返回模拟响应 |
| 认证失败 | AuthenticationRequest 无 token | 记录警告，命令仍可发送 |
| 命令发送失败 | WS 发送/接收异常 | 记录错误并计数，返回 None |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| connect | 是 | 建立 WS 连接 + 插件认证；失败降级模拟 |
| disconnect | 是 | 关闭 WS 连接并清状态 |

## 8. 领域状态说明
- 状态项：`_connected`、`_mock`、`_auth_token`、`_model_name`、`_stats`（commands_sent/errors）
- 持久化：无
- 恢复：connect 重建连接；断线后由调用方重新 connect
"""
import time
import json
import logging
import threading
from typing import Optional, Dict, Any, List

from src.shared.events import VTS_CONNECTED, VTS_DISCONNECTED

logger = logging.getLogger(__name__)


class VTSAdapter:
    """VTS 适配器 — 对接 VTube Studio WebSocket API。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 8001,
                 plugin_name: str = "FutureScene", plugin_dev: str = "FutureScene",
                 event_bus=None):
        self._host = host
        self._port = port
        self._plugin_name = plugin_name
        self._plugin_dev = plugin_dev
        self._event_bus = event_bus
        self._connected = False
        self._ws = None
        self._mock = False
        self._auth_token: Optional[str] = None
        self._model_name: Optional[str] = None
        self._lock = threading.Lock()
        self._stats = {"commands_sent": 0, "errors": 0}
        logger.info("[VTSAdapter] 初始化完成 (host=%s:%d)", host, port)

    def connect(self) -> bool:
        try:
            import websocket
            url = f"ws://{self._host}:{self._port}"
            self._ws = websocket.create_connection(url, timeout=5)
            self._connected = True
            self._mock = False
            logger.info("[VTSAdapter] 已连接 VTube Studio")
            self._authenticate()
            self._publish(VTS_CONNECTED)
            return True
        except ImportError:
            logger.warning("[VTSAdapter] websocket 库未安装，降级为模拟模式")
            self._connected = True
            self._mock = True
            self._publish(VTS_CONNECTED)
            return True
        except Exception as e:
            # 外部软件未运行/不可达 → 降级为模拟模式（返回模拟响应，不阻断链路）
            logger.warning("[VTSAdapter] 连接失败 (%s)，降级为模拟模式", e)
            self._connected = True
            self._mock = True
            self._publish(VTS_CONNECTED)
            return True

    def _authenticate(self) -> None:
        if self._ws is None:
            return
        try:
            req = {"apiName": "VTubeStudioPublicAPI", "apiVersion": "1.0",
                   "requestID": "auth", "messageType": "AuthenticationRequest",
                   "data": {"pluginName": self._plugin_name,
                            "pluginDeveloper": self._plugin_dev}}
            self._ws.send(json.dumps(req))
            resp = json.loads(self._ws.recv())
            self._auth_token = resp.get("data", {}).get("authenticationToken")
            logger.info("[VTSAdapter] 认证成功")
        except Exception as e:
            logger.warning("[VTSAdapter] 认证失败: %s", e)

    def disconnect(self) -> None:
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._connected = False
        self._ws = None
        self._mock = False
        self._publish(VTS_DISCONNECTED)
        logger.info("[VTSAdapter] 已断开")

    def is_connected(self) -> bool:
        return self._connected

    def _publish(self, event: str, **data):
        if self._event_bus:
            try:
                self._event_bus.publish(event, **data)
            except Exception as e:
                logger.debug("[VTSAdapter] 发布事件失败: %s", e)

    def _send_command(self, message_type: str, data: Dict[str, Any]) -> Optional[Dict]:
        if not self._connected:
            logger.warning("[VTSAdapter] 未连接")
            return None
        req = {"apiName": "VTubeStudioPublicAPI", "apiVersion": "1.0",
               "requestID": str(int(time.time() * 1000)),
               "messageType": message_type, "data": data}
        with self._lock:
            self._stats["commands_sent"] += 1
        if self._mock:
            return {"data": {}, "error": None}  # 模拟响应
        if self._ws:
            try:
                self._ws.send(json.dumps(req))
                return json.loads(self._ws.recv())
            except Exception as e:
                logger.error("[VTSAdapter] 命令发送失败: %s", e)
                self._stats["errors"] += 1
        return None

    def set_parameter(self, param_id: str, value: float,
                      weight: float = 1.0) -> bool:
        self._send_command("InjectParameterDataRequest", {
            "parameterValues": [{"id": param_id,
                                 "value": max(0.0, min(1.0, value)),
                                 "weight": max(0.0, min(1.0, weight))}]})
        return True

    def set_parameters(self, params: List[Dict[str, Any]]) -> bool:
        values = [{"id": p["id"],
                   "value": max(0.0, min(1.0, p["value"])),
                   "weight": max(0.0, min(1.0, p.get("weight", 1.0)))}
                  for p in params]
        self._send_command("InjectParameterDataRequest",
                           {"parameterValues": values})
        return True

    def get_model_info(self) -> Optional[Dict[str, Any]]:
        resp = self._send_command("CurrentModelInformationRequest", {})
        if resp is None:
            return None
        data = resp.get("data", {})
        self._model_name = data.get("modelName", self._model_name)
        return {"model_name": data.get("modelName", ""),
                "model_id": data.get("modelID", ""),
                "vts_folder": data.get("vtsFolderName", "")}

    def trigger_hotkey(self, hotkey_id: str) -> bool:
        self._send_command("HotkeyTriggerRequest", {"hotkeyID": hotkey_id})
        return True

    def set_expression(self, expression_name: str,
                       intensity: float = 1.0) -> bool:
        if self.trigger_hotkey(expression_name):
            return True
        return self.set_parameter(expression_name, intensity)

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {**self._stats, "connected": self._connected,
                    "model_name": self._model_name,
                    "host": f"{self._host}:{self._port}"}