"""qq_adapter.py — QQ 机器人适配器（三平台适配器域）

通过 QQ 开放平台官方接口接入 QQ 群聊/单聊/频道。
AccessToken 鉴权 + WebSocket 长连接接收事件，消息转发到 EventBus。

接入：POST /app/getAppAccessToken 获取 token → GET /gateway 获取 WS 地址 →
Identify 鉴权 → 心跳 → 接收事件。

# 模块内容清单 — qq_adapter

## 1. 模块身份标识
- 所属调度官：platform
- 能力名：adapter:qq_connect / adapter:qq_send_group / adapter:qq_send_c2c / adapter:qq_send_channel

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| appid | 是 | 无 | str，非空 | QQ 开放平台机器人 AppID |
| secret | 是 | 无 | str，非空 | QQ 开放平台机器人 ClientSecret |
| intents | 否 | DEFAULT_INTENTS | int（位掩码） | 订阅事件意图（群聊+单聊+频道） |

## 3. 输入契约
- 输入格式：`connect()` / `send_group_message(group_openid, content, msg_type, msg_id)` / `send_c2c_message(openid, content, msg_type, msg_id)` / `send_channel_message(channel_id, content, msg_id)`
- group_openid / openid / channel_id：必填，str，接收方标识
- content：必填，str，消息内容
- msg_type：可选，int（0=文本）；msg_id：可选，str（被动回复时回填）

## 4. 输出契约
- 成功：`connect()` 返回 `True`；`send_*()` 返回 `{"success": True, "message_id": str}`；`get_stats()` 返回 dict
- 失败：`connect()` 返回 `False`（缺凭证/token 失败）；`send_*()` 返回 `{"success": False, "reason": str}`（token_failed / http_xxx / 异常信息）
- 事件：发布 `qq:connected / qq:disconnected / qq:group_message / qq:c2c_message / qq:channel_message`

## 5. 依赖声明
- 外部服务：QQ 开放平台 API（`api.bot.qq.com`、`bots.qq.com`）
- 内部模块：`websockets` 库、`requests` 库（任一缺失降级模拟模式）、`src/shared/events`、event_bus（可选）
- 预先配置：appid + secret 必须存在，否则 connect 返回 False

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 缺凭证 | appid/secret 为空 | connect 返回 False，禁止运行 |
| 依赖缺失 | websockets/requests 未安装 | 降级模拟模式 |
| Token 获取失败 | getAppAccessToken 非 200 或无 token | connect 返回 False |
| 发送失败 | HTTP 非 2xx | send_* 返回 success=False + reason |
| 心跳超时 | 心跳 ACK 超时 | 自动断开并指数退避重连 |
| 会话失效 | OP_INVALID_SESSION | 清 session 重新 Identify |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| connect | 是 | 获取 token/gateway，启动 WS 线程 + 心跳线程 |
| disconnect | 是 | 停止线程、关闭 WS、清 session |

## 8. 领域状态说明
- 状态项：`_connected`、`_session_id`、`_seq`、`_access_token`、`_token_expires_at`、`_ws_url`、`_msg_count`、`_last_event_time`
- 持久化：无
- 恢复：connect 重建连接；断线由 WSReconnectPolicy 指数退避重连（5s→30s）
"""
import json
import logging
import threading
import time
import asyncio
import platform
from typing import Dict, List, Optional, Any

from src.shared.events import (QQ_CONNECTED, QQ_DISCONNECTED, QQ_GROUP_MESSAGE,
                               QQ_C2C_MESSAGE, QQ_CHANNEL_MESSAGE)

logger = logging.getLogger(__name__)

try:
    import websockets
    _HAS_WS = True
except ImportError:
    _HAS_WS = False

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

# ========== QQ 机器人 API 常量 ==========
QQ_API_BASE = "https://api.bot.qq.com"
QQ_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
QQ_GATEWAY_URL = f"{QQ_API_BASE}/gateway"

INTENT_PUBLIC_GUILD_MESSAGES = 1 << 30  # 频道@机器人消息
INTENT_GROUP_AND_C2C_EVENT = 1 << 25    # 群聊@消息 + 单聊消息
INTENT_GUILDS = 1 << 0
DEFAULT_INTENTS = INTENT_GROUP_AND_C2C_EVENT | INTENT_PUBLIC_GUILD_MESSAGES | INTENT_GUILDS

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11


class WSReconnectPolicy:
    """WebSocket 重连：指数退避 + jitter。"""

    def __init__(self, base_delay=5, max_delay=30):
        self._base = base_delay
        self._max = max_delay
        self.reconnect_count = 0
        self._heartbeat_ack = 0.0

    def get_delay(self) -> float:
        import random
        base = min(self._base * (2 ** self.reconnect_count), self._max)
        jitter = base * 0.3 * (random.random() * 2 - 1)
        return max(1.0, base + jitter)

    def on_connected(self):
        self.reconnect_count = 0

    def on_disconnected(self):
        self.reconnect_count += 1

    def on_heartbeat_ack(self):
        self._heartbeat_ack = time.time()

    def on_heartbeat_sent(self):
        self._heartbeat_ack = 0.0

    def is_heartbeat_timeout(self, interval_ms: int) -> bool:
        if self._heartbeat_ack == 0.0:
            return False
        return (time.time() - self._heartbeat_ack) > (interval_ms / 1000) * 2


class QQBotAdapter:
    """QQ 机器人适配器。"""

    def __init__(self, config: Optional[Dict] = None, event_bus=None,
                 appid: str = "", secret: str = ""):
        self.name = "qq_bot"
        self._config = config or {}
        self._event_bus = event_bus
        self._appid = appid or self._config.get("bot_appid",
                                                self._config.get("app_id", ""))
        self._secret = secret or self._config.get("secret",
                                                  self._config.get("client_secret", ""))
        self._connected = False
        self._ws = None
        self._loop = None
        self._ws_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._running = False
        self._access_token = ""
        self._token_expires_at = 0.0
        self._ws_url = ""
        self._session_id = ""
        self._seq = 0
        self._heartbeat_interval = 45000
        self._reconnect_policy = WSReconnectPolicy(base_delay=5, max_delay=30)
        self._msg_count = 0
        self._last_event_time = 0.0
        logger.info("[QQBot] 初始化完成 (appid=%s)", self._appid[:6] if self._appid else "")

    # ========== 生命周期 ==========

    def connect(self) -> bool:
        if not self._appid or not self._secret:
            logger.error("[QQBot] 缺少 appId 或 clientSecret")
            return False
        if not _HAS_WS or not _HAS_REQUESTS:
            logger.warning("[QQBot] 依赖缺失（websockets/requests），降级为模拟模式")
            self._connected = True
            self._publish(QQ_CONNECTED)
            return True
        if not self._refresh_access_token():
            return False
        if not self._get_gateway():
            return False
        self._running = True
        self._ws_thread = threading.Thread(target=self._ws_run_loop,
                                           daemon=True, name="QQBot-WS")
        self._ws_thread.start()
        for _ in range(20):
            if not self._running:
                return False
            if self._session_id:
                logger.info("[QQBot] 连接成功, session=%s...", self._session_id[:8])
                self._connected = True
                self._publish(QQ_CONNECTED)
                return True
            time.sleep(0.5)
        logger.warning("[QQBot] 连接超时，后台线程仍在运行")
        self._connected = True
        self._publish(QQ_CONNECTED)
        return True

    def disconnect(self):
        self._running = False
        if self._ws and self._loop:
            try:
                asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
            except Exception:
                pass
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=5)
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2)
        self._ws = None
        self._session_id = ""
        self._connected = False
        self._publish(QQ_DISCONNECTED)
        logger.info("[QQBot] 已断开连接")

    # ========== AccessToken / Gateway ==========

    def _refresh_access_token(self) -> bool:
        if not self._appid or not self._secret:
            return False
        if self._access_token and time.time() < self._token_expires_at - 60:
            return True
        try:
            resp = requests.post(QQ_TOKEN_URL,
                                 json={"appId": self._appid,
                                       "clientSecret": self._secret},
                                 timeout=10)
            if resp.status_code != 200:
                logger.error("[QQBot] 获取 token 失败: HTTP %s", resp.status_code)
                return False
            data = resp.json()
            self._access_token = data.get("access_token", "")
            self._token_expires_at = time.time() + int(data.get("expires_in", 7200))
            if not self._access_token:
                logger.error("[QQBot] 响应中无 access_token")
                return False
            logger.info("[QQBot] access_token 获取成功")
            return True
        except Exception as e:
            logger.error("[QQBot] 获取 access_token 异常: %s", e)
            return False

    def _get_auth_header(self) -> Dict[str, str]:
        return {"Authorization": "QQBot " + self._access_token}

    def _get_gateway(self) -> bool:
        if not self._refresh_access_token():
            return False
        try:
            resp = requests.get(QQ_GATEWAY_URL, headers=self._get_auth_header(),
                                timeout=10)
            if resp.status_code != 200:
                logger.error("[QQBot] 获取 gateway 失败: HTTP %s", resp.status_code)
                return False
            self._ws_url = resp.json().get("url", "")
            return bool(self._ws_url)
        except Exception as e:
            logger.error("[QQBot] 获取 gateway 异常: %s", e)
            return False

    # ========== WebSocket 主循环 ==========

    def _ws_run_loop(self):
        while self._running:
            try:
                if not self._refresh_access_token():
                    self._sleep_interruptible(10)
                    continue
                if not self._get_gateway():
                    self._sleep_interruptible(10)
                    continue
                asyncio.run(self._ws_main())
            except Exception as e:
                logger.error("[QQBot] WebSocket 异常: %s", e)
            if self._running:
                delay = self._reconnect_policy.get_delay()
                self._reconnect_policy.on_disconnected()
                logger.info("[QQBot] %.1fs 后重连", delay)
                self._sleep_interruptible(delay)

    def _sleep_interruptible(self, seconds: float):
        for _ in range(int(seconds * 2)):
            if not self._running:
                break
            time.sleep(0.5)

    async def _ws_main(self):
        self._loop = asyncio.get_event_loop()
        try:
            async with websockets.connect(self._ws_url) as ws:
                self._ws = ws
                async for raw_msg in ws:
                    if not self._running:
                        break
                    try:
                        await self._handle_ws_message(json.loads(raw_msg))
                    except json.JSONDecodeError:
                        logger.warning("[QQBot] 无法解析消息")
                    except Exception as e:
                        logger.error("[QQBot] 处理消息异常: %s", e)
        except Exception as e:
            logger.error("[QQBot] WebSocket 连接异常: %s", e)
        finally:
            self._ws = None

    async def _handle_ws_message(self, msg: dict):
        op = msg.get("op")
        s = msg.get("s")
        d = msg.get("d", {})
        if s is not None and s > 0:
            self._seq = s
        if op == OP_HELLO:
            self._heartbeat_interval = d.get("heartbeat_interval", 45000)
            self._start_heartbeat()
            await self._send_identify()
        elif op == OP_DISPATCH:
            self._last_event_time = time.time()
            self._msg_count += 1
            self._handle_dispatch(msg.get("t", ""), d)
        elif op == OP_HEARTBEAT_ACK:
            self._reconnect_policy.on_heartbeat_ack()
        elif op == OP_RECONNECT:
            self._session_id = ""
            await self._ws.close()
        elif op == OP_INVALID_SESSION:
            self._session_id = ""
            self._token_expires_at = 0
            await self._ws.close()

    async def _send_identify(self):
        if self._session_id:
            payload = {"op": OP_RESUME,
                       "d": {"token": "QQBot " + self._access_token,
                             "session_id": self._session_id,
                             "seq": self._seq}}
        else:
            payload = {"op": OP_IDENTIFY,
                       "d": {"token": "QQBot " + self._access_token,
                             "intents": DEFAULT_INTENTS,
                             "shard": [0, 1],
                             "properties": {"$os": platform.system(),
                                            "$browser": "FutureScene",
                                            "$device": "FutureScene"}}}
        await self._ws.send(json.dumps(payload))

    def _start_heartbeat(self):
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop,
                                                  daemon=True, name="QQBot-HB")
        self._heartbeat_thread.start()

    def _heartbeat_loop(self):
        interval = self._heartbeat_interval / 1000.0
        while self._running and self._ws:
            time.sleep(interval)
            if not self._running or not self._ws:
                break
            if self._token_expires_at > 0 and time.time() > self._token_expires_at - 300:
                self._refresh_access_token()
            if self._reconnect_policy.is_heartbeat_timeout(self._heartbeat_interval):
                logger.warning("[QQBot] 心跳 ACK 超时，触发重连")
                if self._loop and self._loop.is_running() and self._ws:
                    asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
                break
            payload = {"op": OP_HEARTBEAT, "d": self._seq if self._seq > 0 else None}
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(self._ws.send(json.dumps(payload)),
                                                 self._loop)
                self._reconnect_policy.on_heartbeat_sent()

    # ========== 事件处理 ==========

    def _handle_dispatch(self, event_type: str, data: dict):
        if event_type == "GROUP_AT_MESSAGE_CREATE":
            self._on_group_message(data)
        elif event_type == "C2C_MESSAGE_CREATE":
            self._on_c2c_message(data)
        elif event_type == "AT_MESSAGE_CREATE":
            self._on_channel_message(data)
        elif event_type == "READY":
            self._session_id = data.get("session_id", "")
            self._reconnect_policy.on_connected()
            logger.info("[QQBot] Ready! session=%s...", self._session_id[:8])

    def _on_group_message(self, data: dict):
        content = data.get("content", "").strip()
        if content.startswith("@"):
            parts = content.split(None, 1)
            content = parts[1] if len(parts) > 1 else ""
        author = data.get("author", {})
        self._publish(QQ_GROUP_MESSAGE, content=content,
                      author_id=author.get("member_openid", ""),
                      group_id=data.get("group_openid", ""),
                      message_id=data.get("id", ""),
                      timestamp=data.get("timestamp", ""),
                      bot_name=self.name)

    def _on_c2c_message(self, data: dict):
        author = data.get("author", {})
        self._publish(QQ_C2C_MESSAGE, content=data.get("content", "").strip(),
                      author_id=author.get("user_openid", ""),
                      message_id=data.get("id", ""),
                      timestamp=data.get("timestamp", ""),
                      bot_name=self.name)

    def _on_channel_message(self, data: dict):
        author = data.get("author", {})
        self._publish(QQ_CHANNEL_MESSAGE, content=data.get("content", "").strip(),
                      author_id=author.get("id", ""),
                      author_name=author.get("username", ""),
                      channel_id=data.get("channel_id", ""),
                      guild_id=data.get("guild_id", ""),
                      message_id=data.get("id", ""),
                      timestamp=data.get("timestamp", ""))

    def _publish(self, event: str, **data):
        if self._event_bus:
            try:
                self._event_bus.publish(event, **data)
            except Exception as e:
                logger.debug("[QQBot] 发布事件失败: %s", e)

    # ========== 消息发送 API ==========

    def send_group_message(self, group_openid: str, content: str,
                           msg_type: int = 0, msg_id: str = "") -> dict:
        if not self._refresh_access_token():
            return {"success": False, "reason": "token_failed"}
        try:
            payload = {"content": content, "msg_type": msg_type}
            if msg_id:
                payload["msg_id"] = msg_id
            resp = requests.post(f"{QQ_API_BASE}/v2/groups/{group_openid}/messages",
                                 headers={**self._get_auth_header(),
                                          "Content-Type": "application/json"},
                                 json=payload, timeout=10)
            if resp.status_code in (200, 201, 204):
                data = resp.json() if resp.text else {}
                return {"success": True, "message_id": data.get("id", "")}
            return {"success": False, "reason": f"http_{resp.status_code}"}
        except Exception as e:
            return {"success": False, "reason": str(e)}

    def send_c2c_message(self, openid: str, content: str,
                         msg_type: int = 0, msg_id: str = "") -> dict:
        if not self._refresh_access_token():
            return {"success": False, "reason": "token_failed"}
        try:
            payload = {"content": content, "msg_type": msg_type}
            if msg_id:
                payload["msg_id"] = msg_id
            resp = requests.post(f"{QQ_API_BASE}/v2/users/{openid}/messages",
                                 headers={**self._get_auth_header(),
                                          "Content-Type": "application/json"},
                                 json=payload, timeout=10)
            if resp.status_code in (200, 201, 204):
                data = resp.json() if resp.text else {}
                return {"success": True, "message_id": data.get("id", "")}
            return {"success": False, "reason": f"http_{resp.status_code}"}
        except Exception as e:
            return {"success": False, "reason": str(e)}

    def send_channel_message(self, channel_id: str, content: str,
                             msg_id: str = "") -> dict:
        if not self._refresh_access_token():
            return {"success": False, "reason": "token_failed"}
        try:
            payload = {"content": content, "msg_type": 0}
            if msg_id:
                payload["msg_id"] = msg_id
            resp = requests.post(f"{QQ_API_BASE}/channels/{channel_id}/messages",
                                 headers={**self._get_auth_header(),
                                          "Content-Type": "application/json"},
                                 json=payload, timeout=10)
            if resp.status_code in (200, 201, 204):
                data = resp.json() if resp.text else {}
                return {"success": True, "message_id": data.get("id", "")}
            return {"success": False, "reason": f"http_{resp.status_code}"}
        except Exception as e:
            return {"success": False, "reason": str(e)}

    # ========== 状态 ==========

    def is_connected(self) -> bool:
        return self._connected

    def get_stats(self) -> Dict[str, Any]:
        return {"connected": self._connected, "msg_count": self._msg_count,
                "session_id": self._session_id,
                "last_event_time": self._last_event_time}