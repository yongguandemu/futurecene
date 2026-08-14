"""test_bilibili_orchestrator.py — B站调度官单测（mock 外部 API/WS，规格书 1048 行）"""
import asyncio

from src.orchestrators.bilibili_orchestrator import normalizer
from src.orchestrators.bilibili_orchestrator.bilibili_orchestrator import BilibiliOrchestrator
from src.orchestrators.bilibili_orchestrator.connector import sign_request
from src.orchestrators.bilibili_orchestrator import registry
from src.shared.event_bus import EventBus
from src.shared.events import (
    AUDIENCE_ENTERED,
    BILIBILI_CONNECTED,
    BILIBILI_DISCONNECTED,
    DANMAKU_RECEIVED,
    GIFT_RECEIVED,
)


class FakeConnector:
    """mock WebSocket 连接器。"""

    def __init__(self, connect_fail=False):
        self.connected = False
        self.connect_fail = connect_fail
        self.sent = []

    async def connect(self, room_id=""):
        if self.connect_fail:
            raise RuntimeError("connect refused")
        self.connected = True

    async def disconnect(self):
        self.connected = False

    def send_danmaku(self, text):
        self.sent.append(text)
        return {"sent": True}

    def get_stream_code(self):
        return {"rtmp_url": "rtmp://example", "stream_key": "key123"}


def _make_orchestrator(connector=None):
    bus = EventBus()
    bus.reset()
    orch = BilibiliOrchestrator(event_bus=bus, connector=connector or FakeConnector())
    orch.start()
    return orch, bus


def test_capabilities_from_registry():
    orch, _ = _make_orchestrator()
    assert orch.capabilities() == registry.capabilities() == [
        "bilibili:connect", "bilibili:disconnect",
        "bilibili:send_message", "bilibili:get_stream_code",
    ]


def test_sign_request_headers():
    headers = sign_request("ak123", "sk123", {"app_id": "1", "room_id": "2"})
    assert headers["x-bili-accesskeyid"] == "ak123"
    assert headers["x-bili-signature-method"] == "HMAC-SHA256"
    assert headers["x-bili-signature-version"] == "1.0"
    assert headers["x-bili-signature-nonce"]
    assert headers["x-bili-timestamp"]
    assert headers["x-bili-signature"]  # base64 签名非空
    import base64
    base64.b64decode(headers["x-bili-signature"])  # 合法 base64


def test_normalize_and_publish_danmaku():
    bus = EventBus()
    bus.reset()
    seen = {}
    bus.subscribe(DANMAKU_RECEIVED, lambda event, **kw: seen.update(kw))
    event = normalizer.normalize({"cmd": "DANMU_MSG",
                                  "data": {"text": "你好呀", "user_name": "观众A",
                                           "user_id": 1001}})
    assert event is not None and event.event_type == "danmaku"
    normalizer.publish(bus, event)
    assert seen["content"] == "你好呀"
    assert seen["user_name"] == "观众A"


def test_normalize_gift_and_interact():
    gift = normalizer.normalize({"cmd": "SEND_GIFT",
                                 "data": {"gift_name": "辣条", "num": 5, "user_name": "B"}})
    assert gift.event_type == "gift" and gift.content == "辣条 x5"
    interact = normalizer.normalize({"cmd": "INTERACT_WORD", "data": {"user_name": "C"}})
    assert interact.event_type == "interact"
    # interact → audience:entered 事件
    bus = EventBus()
    bus.reset()
    seen = {}
    bus.subscribe(AUDIENCE_ENTERED, lambda event, **kw: seen.update(kw))
    normalizer.publish(bus, interact)
    assert "user_name" in seen


def test_normalize_unknown_returns_none():
    assert normalizer.normalize({"cmd": "SOMETHING_NEW", "data": {}}) is None


def test_handle_connect_publishes_event():
    connector = FakeConnector()
    orch, bus = _make_orchestrator(connector=connector)
    seen = {}
    bus.subscribe(BILIBILI_CONNECTED, lambda event, **kw: seen.update(kw))
    result = asyncio.run(orch.handle({"capability": "bilibili:connect", "payload": {}}))
    assert result["ok"] is True
    assert result["data"] == {"connected": True}
    assert seen == {"room_id": ""}


def test_handle_connect_failure():
    orch, _ = _make_orchestrator(connector=FakeConnector(connect_fail=True))
    result = asyncio.run(orch.handle({"capability": "bilibili:connect", "payload": {}}))
    assert result["ok"] is False
    assert "connect refused" in result["error"]


def test_handle_disconnect():
    orch, bus = _make_orchestrator()
    seen = {}
    bus.subscribe(BILIBILI_DISCONNECTED, lambda event, **kw: seen.update(kw))
    result = asyncio.run(orch.handle({"capability": "bilibili:disconnect", "payload": {}}))
    assert result["ok"] is True
    assert seen == {}


def test_handle_send_message_and_stream_code():
    connector = FakeConnector()
    orch, _ = _make_orchestrator(connector=connector)
    r1 = asyncio.run(orch.handle({"capability": "bilibili:send_message", "payload": {"text": "hi"}}))
    assert r1["ok"] is True and connector.sent == ["hi"]
    r2 = asyncio.run(orch.handle({"capability": "bilibili:get_stream_code", "payload": {}}))
    assert r2["data"]["rtmp_url"] == "rtmp://example"


def test_handle_unknown_capability():
    orch, _ = _make_orchestrator()
    result = asyncio.run(orch.handle({"capability": "bilibili:unknown", "payload": {}}))
    assert result["ok"] is False


def test_health():
    orch, _ = _make_orchestrator(connector=FakeConnector())
    assert orch.health()["status"] == "degraded"  # 未连接
    orch._connector.connected = True
    assert orch.health()["status"] == "ok"


def test_on_message_publishes_normalized():
    orch, bus = _make_orchestrator()
    seen = {}
    bus.subscribe(DANMAKU_RECEIVED, lambda event, **kw: seen.update(kw))
    orch._on_message({"cmd": "DANMU_MSG",
                      "data": {"text": "弹幕", "user_name": "D", "user_id": 1}})
    assert seen["content"] == "弹幕"
