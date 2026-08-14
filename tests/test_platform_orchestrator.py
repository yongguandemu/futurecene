"""test_platform_orchestrator.py — 三平台适配器调度官单测（mock 三大适配器）"""
import asyncio

from src.orchestrators.platform_orchestrator import registry
from src.orchestrators.platform_orchestrator.platform_orchestrator import (
    PlatformOrchestrator,
)
from src.shared.event_bus import EventBus


class FakeQQ:
    def __init__(self):
        self.connected = False

    def connect(self):
        self.connected = True
        return True

    def disconnect(self):
        self.connected = False

    def send_group_message(self, group_openid, content, msg_type=0, msg_id=""):
        return {"success": self.connected, "detail": content}

    def send_c2c_message(self, openid, content, msg_type=0, msg_id=""):
        return {"success": self.connected, "detail": content}

    def send_channel_message(self, channel_id, content, msg_id=""):
        return {"success": self.connected, "detail": content}

    def get_stats(self):
        return {"connected": self.connected}


class FakeOBS:
    def __init__(self):
        self.connected = False
        self.streaming = False

    def connect(self):
        self.connected = True
        return True

    def disconnect(self):
        self.connected = False

    def start_streaming(self, server=None, key=None):
        self.streaming = True
        return True

    def stop_streaming(self):
        self.streaming = False
        return True

    def is_streaming(self):
        return self.streaming

    def switch_scene(self, scene):
        return True

    def get_scenes(self):
        return ["场景A", "场景B"]

    def get_screenshot(self, width=800, height=450):
        return {"width": width, "height": height}

    def get_status(self):
        return {"connected": self.connected, "streaming": self.streaming}


class FakeVTS:
    def __init__(self):
        self.connected = False

    def connect(self):
        self.connected = True
        return True

    def disconnect(self):
        self.connected = False

    def set_parameter(self, param_id, value, weight=1.0):
        return True

    def set_expression(self, expression, intensity=1.0):
        return True

    def trigger_hotkey(self, hotkey_id):
        return True

    def get_model_info(self):
        return {"model": "Yuki"}

    def get_stats(self):
        return {"connected": self.connected}


def _make():
    bus = EventBus()
    bus.reset()
    orch = PlatformOrchestrator(event_bus=bus, qq=FakeQQ(), obs=FakeOBS(),
                                 vts=FakeVTS())
    orch.start()
    return orch, bus


def test_capabilities_from_registry():
    orch, _ = _make()
    caps = orch.capabilities()
    assert caps == registry.capabilities()
    assert "adapter:connect" in caps and "adapter:obs_stream_start" in caps


def test_connect_and_status():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "adapter:connect",
                                 "payload": {"platform": "qq"}}))
    assert r["ok"] is True
    r = asyncio.run(orch.handle({"capability": "adapter:status",
                                 "payload": {}}))
    assert r["data"]["qq"]["connected"] is True


def test_connect_all():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "adapter:connect",
                                 "payload": {}}))
    assert r["ok"] is True
    assert r["data"]["qq"]["connected"] is True
    assert r["data"]["obs"]["connected"] is True
    assert r["data"]["vts"]["connected"] is True


def test_obs_stream_start_stop():
    orch, _ = _make()
    asyncio.run(orch.handle({"capability": "adapter:connect",
                             "payload": {"platform": "obs"}}))
    r = asyncio.run(orch.handle({"capability": "adapter:obs_stream_start",
                                 "payload": {"server": "rtmp://x", "key": "k"}}))
    assert r["data"]["streaming"] is True
    r = asyncio.run(orch.handle({"capability": "adapter:obs_stream_stop",
                                 "payload": {}}))
    assert r["data"]["streaming"] is False


def test_obs_scenes_and_screenshot():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "adapter:obs_scenes",
                                 "payload": {}}))
    assert len(r["data"]["scenes"]) == 2
    r = asyncio.run(orch.handle({"capability": "adapter:obs_screenshot",
                                 "payload": {"width": 640, "height": 360}}))
    assert r["data"]["width"] == 640


def test_qq_send_group():
    orch, _ = _make()
    asyncio.run(orch.handle({"capability": "adapter:connect",
                             "payload": {"platform": "qq"}}))
    r = asyncio.run(orch.handle({"capability": "adapter:qq_send_group",
                                 "payload": {"group_openid": "g1",
                                             "content": "你好"}}))
    assert r["ok"] is True


def test_vts_param_and_model():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "adapter:vts_param",
                                 "payload": {"param_id": "MouthOpen",
                                             "value": 0.5}}))
    assert r["ok"] is True
    r = asyncio.run(orch.handle({"capability": "adapter:vts_model",
                                 "payload": {}}))
    assert r["data"]["model"] == "Yuki"


def test_unknown_capability():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "adapter:unknown",
                                 "payload": {}}))
    assert r["ok"] is False


def test_health():
    orch, _ = _make()
    assert orch.health()["status"] == "ok"