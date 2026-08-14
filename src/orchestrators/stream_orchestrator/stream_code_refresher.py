"""stream_code_refresher.py — B站推流码刷新器（无人值守直播域）

用登录 Cookie 调用 B站网页接口（room/v1/Room/startLive）获取最新推流地址，
返回 (RTMP server, key)。凭证从 Cookie 串 / .env 读取，不写死在代码里；
Cookie / 推流码为敏感信息，不落日志。

# 模块内容清单 — stream_code_refresher

## 1. 模块身份标识
- 所属调度官：stream
- 能力名：stream:fetch_code

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| cookie | 否 | "" | str | B站 Cookie 串（含 SESSDATA / bili_jct） |
| cookie_file | 否 | deploy/bilibili_cookie.json | str | 凭证 JSON 文件路径（含 cookie/room_id/identity_code） |
| room_id | 否 | 0 | int，>0 | B站直播间 ID |
| identity_code | 否 | "" | str | 身份码（开播校验用） |

## 3. 输入契约
- 输入格式：`fetch_stream_code()` -> (server, key)
- 无参数；凭证在构造时从 cookie / cookie_file / 默认文件加载（优先级：cookie_file > cookie > 默认文件）

## 4. 输出契约
- 成功：`fetch_stream_code()` 返回 `(server, key)` 元组（str, str）
- 失败：抛 `RuntimeError`（缺 Cookie / requests 缺失 / HTTP 非 200 / 业务错误 / 响应缺推流地址）
- 事件：无

## 5. 依赖声明
- 外部服务：B站 startLive API（`api.live.bilibili.com/room/v1/Room/startLive`）
- 内部模块：`requests` 库（缺失抛 RuntimeError）、`src/shared/config_loader.PROJECT_ROOT`
- 预先配置：Cookie（SESSDATA / bili_jct）必须存在，否则 fetch_stream_code 抛异常

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 缺 Cookie | SESSDATA / bili_jct 为空 | 抛 RuntimeError，提示配置凭证 |
| requests 缺失 | requests 未安装 | 抛 RuntimeError，提示安装 |
| HTTP 错误 | startLive 非 200 | 抛 RuntimeError（含状态码） |
| 业务错误 | 响应 code != 0 | 抛 RuntimeError（含 message） |
| 缺推流地址 | 响应无 addr/code | 抛 RuntimeError |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start/stop | 否 | 无（无长驻资源，每次调用实时刷新） |

## 8. 领域状态说明
- 状态项：`_room_id`、`_identity_code`、`_cookie`、`_sessdata`、`_csrf`
- 持久化：无（凭证从文件/参数加载，不落盘）
- 恢复：构造时加载凭证；每次 fetch_stream_code 实时请求最新推流地址
"""
import json
import os
import logging
from pathlib import Path
from typing import Optional, Tuple

from src.shared.config_loader import PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_COOKIE_FILE = PROJECT_ROOT / "deploy" / "bilibili_cookie.json"
START_LIVE_URL = "https://api.live.bilibili.com/room/v1/Room/startLive"
_COOKIE_FIELDS = ("SESSDATA", "DedeUserID", "DedeUserID__ckMd5", "bili_jct", "buvid3")


def _cookie_value(cookie_str: str, key: str) -> str:
    for part in (cookie_str or "").split(";"):
        part = part.strip()
        if part.startswith(key + "="):
            return part[len(key) + 1:].strip()
    return ""


class StreamCodeRefresher:
    """推流码刷新器：加载 Cookie 凭证，刷新并解析 B站推流地址。"""

    def __init__(self, cookie: str = "", cookie_file: Optional[str] = None,
                 room_id: int = 0, identity_code: str = ""):
        self._room_id = int(room_id)
        self._identity_code = identity_code
        self._cookie = ""
        self._sessdata = ""
        self._csrf = ""
        if cookie_file:
            self._load_from_file(cookie_file)
        if not self._cookie and cookie:
            self._set_cookie(cookie)
        if not self._cookie and DEFAULT_COOKIE_FILE.exists():
            try:
                self._load_from_file(str(DEFAULT_COOKIE_FILE))
            except Exception as e:
                logger.warning("[StreamCodeRefresher] 默认凭证文件加载失败: %s", e)

    def _set_cookie(self, cookie: str) -> None:
        self._cookie = cookie or ""
        self._sessdata = _cookie_value(self._cookie, "SESSDATA")
        self._csrf = _cookie_value(self._cookie, "bili_jct")

    def _load_from_file(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        ck = data.get("cookie") or {}
        parts = []
        for key in _COOKIE_FIELDS:
            if key in ck:
                parts.append("{}={}".format(key, ck[key]))
        self._set_cookie("; ".join(parts))
        self._identity_code = str(data.get("identity_code") or self._identity_code or "")
        rid = data.get("room_id")
        if rid:
            self._room_id = int(rid)
        logger.info("[StreamCodeRefresher] 已加载推流凭证 (room_id=%s, identity_len=%s)",
                    self._room_id, len(self._identity_code))

    def fetch_stream_code(self) -> Tuple[str, str]:
        """调用 B站 startLive 接口获取推流地址，返回 (server, key)。"""
        if not self._sessdata or not self._csrf:
            raise RuntimeError("缺少 B站 Cookie（SESSDATA / bili_jct）")
        try:
            import requests
        except ImportError:
            raise RuntimeError("requests 库未安装")
        params = {
            "platform": "pc", "room_id": self._room_id,
            "identity_code": self._identity_code,
            "csrf": self._csrf,
        }
        headers = {"Cookie": self._cookie, "Referer": "https://live.bilibili.com/"}
        resp = requests.post(START_LIVE_URL, params=params, headers=headers,
                             timeout=15)
        if resp.status_code != 200:
            raise RuntimeError("startLive HTTP {}".format(resp.status_code))
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError("startLive 业务错误: {}".format(data.get("message")))
        rtmp = data.get("data", {}).get("rtmp", {})
        server = rtmp.get("addr", "")
        key_ = rtmp.get("code", "")
        if not server or not key_:
            raise RuntimeError("startLive 响应缺少推流地址")
        logger.info("[StreamCodeRefresher] 获取推流地址成功 -> %s (key 长度=%d)",
                    self._extract_host(server), len(key_))
        return server, key_

    @staticmethod
    def _extract_host(server: str) -> str:
        try:
            if "://" in server:
                return server.split("/")[2]
            return server
        except (AttributeError, IndexError, TypeError):
            return str(server)