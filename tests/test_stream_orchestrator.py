"""test_stream_orchestrator.py — 无人值守直播调度官单测（mock 推流器/推流码/启动器）"""
import asyncio

from src.orchestrators.stream_orchestrator import registry
from src.orchestrators.stream_orchestrator.stream_orchestrator import StreamOrchestrator
from src.shared.event_bus import EventBus


class FakeRefresher:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def fetch_stream_code(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("Cookie 过期")
        return "rtmp://live.example.com/app", "streamkey123"


class FakeStreamer:
    def __init__(self, fail_start=False):
        self.fail_start = fail_start
        self.alive = False
        self.started = 0
        self.stopped = 0

    def start(self, server, key):
        if self.fail_start:
            return False
        self.alive = True
        self.started += 1
        return True

    def stop(self):
        self.alive = False
        self.stopped += 1

    def is_alive(self):
        return self.alive

    def get_status(self):
        return {"alive": self.alive, "started": self.started}


class FakeLauncher:
    def __init__(self):
        self.launched = {}
        self.closed = False

    def register_template(self, name, path, args=None, cwd=None, env=None,
                          auto_start=False):
        self.launched[name] = path

    def launch(self, name, path=None, args=None, cwd=None, env=None):
        if not path:
            return False
        self.launched[name] = path
        return True

    def terminate(self, name):
        return name in self.launched

    def get_pid(self, name):
        return 1234 if name in self.launched else None

    def list_launched(self):
        return [{"name": k} for k in self.launched]

    def close(self):
        self.closed = True


def _make(refresher=None, streamer=None, launcher=None):
    bus = EventBus()
    bus.reset()
    orch = StreamOrchestrator(event_bus=bus, refresher=refresher or FakeRefresher(),
                              streamer=streamer or FakeStreamer(),
                              launcher=launcher or FakeLauncher())
    orch.start()
    return orch, bus


def test_capabilities_from_registry():
    orch, _ = _make()
    caps = orch.capabilities()
    assert caps == registry.capabilities()
    assert "stream:start" in caps and "stream:app_register" in caps


def test_start_live_ok():
    orch, bus = _make()
    seen = {}
    bus.subscribe("stream:state_changed", lambda event, **kw: seen.update(kw))
    r = asyncio.run(orch.handle({"capability": "stream:start", "payload": {}}))
    assert r["ok"] is True
    assert r["data"]["state"] == "live"
    assert seen.get("state") == "live"
    orch.stop()  # 停心跳线程


def test_double_start_rejected():
    orch, _ = _make()
    asyncio.run(orch.handle({"capability": "stream:start", "payload": {}}))
    r = asyncio.run(orch.handle({"capability": "stream:start", "payload": {}}))
    assert r["ok"] is False
    orch.stop()


def test_start_fails_when_refresher_fails():
    orch, _ = _make(refresher=FakeRefresher(fail=True))
    r = asyncio.run(orch.handle({"capability": "stream:start", "payload": {}}))
    assert r["ok"] is False
    r = asyncio.run(orch.handle({"capability": "stream:state", "payload": {}}))
    assert r["data"]["state"] == "failed"


def test_start_fails_when_streamer_fails():
    orch, _ = _make(streamer=FakeStreamer(fail_start=True))
    r = asyncio.run(orch.handle({"capability": "stream:start", "payload": {}}))
    assert r["ok"] is False


def test_stop_live():
    orch, _ = _make()
    asyncio.run(orch.handle({"capability": "stream:start", "payload": {}}))
    r = asyncio.run(orch.handle({"capability": "stream:stop", "payload": {}}))
    assert r["ok"] is True and r["data"]["state"] == "idle"
    orch.stop()


def test_fetch_code():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "stream:fetch_code", "payload": {}}))
    assert r["ok"] is True
    assert r["data"]["key_len"] == 12  # "streamkey123"


def test_app_register_and_launch():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "stream:app_register",
                                 "payload": {"name": "obs", "path": "OBS.exe"}}))
    assert r["ok"] is True
    r = asyncio.run(orch.handle({"capability": "stream:app_list", "payload": {}}))
    assert r["data"] == [{"name": "obs"}]
    orch.stop()


def test_launch_app_without_path_fails():
    orch, _ = _make(launcher=FakeLauncher())
    r = asyncio.run(orch.handle({"capability": "stream:launch_app",
                                 "payload": {"name": "ghost"}}))
    assert r["ok"] is False
    orch.stop()


def test_unknown_capability():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "stream:unknown", "payload": {}}))
    assert r["ok"] is False
    orch.stop()


def test_health_and_get_status():
    orch, _ = _make()
    assert orch.health()["status"] == "ok"
    st = orch.get_status()
    assert st["state"] == "idle"
    orch.stop()