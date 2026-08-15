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


def _make_with_config(cfg):
    bus = EventBus()
    bus.reset()
    orch = StreamOrchestrator(event_bus=bus, config=cfg,
                              refresher=FakeRefresher(),
                              streamer=FakeStreamer(),
                              launcher=FakeLauncher())
    orch.start()
    return orch, bus


# ---------- 手动推流码（config rtmp_server/rtmp_key） ----------

def test_manual_stream_code_preferred():
    """配置了手动推流码 → start 直接用，不触达 refresher（无直播间也能推）。"""
    orch, _ = _make_with_config({"rtmp_server": "rtmp://manual.example.com/app",
                                 "rtmp_key": "manualkey"})
    r = asyncio.run(orch.handle({"capability": "stream:start", "payload": {}}))
    assert r["ok"] is True
    assert orch._refresher.calls == 0  # 手动码优先，未调自动刷新


def test_manual_stream_code_falls_back_to_refresher():
    """未配置手动码 → 走 refresher 自动刷新（直播间 + Cookie 模式）。"""
    orch, _ = _make_with_config({"rtmp_server": "", "rtmp_key": ""})
    r = asyncio.run(orch.handle({"capability": "stream:start", "payload": {}}))
    assert r["ok"] is True
    assert orch._refresher.calls == 1


def test_manual_stream_code_empty_fails_cleanly():
    """手动码为空 + refresher 失败 → 开播失败，透传 refresher 错误信息。"""
    orch, _ = _make_with_config({"rtmp_server": "", "rtmp_key": ""})
    orch._refresher = FakeRefresher(fail=True)
    r = asyncio.run(orch.handle({"capability": "stream:start", "payload": {}}))
    assert r["ok"] is False and "Cookie 过期" in r["error"]


# ---------- HeadlessStreamer 多内容源 ----------

def test_headless_live2d_cmd_uses_image2pipe():
    from src.orchestrators.stream_orchestrator.headless_streamer import HeadlessStreamer
    s = HeadlessStreamer(source_type="live2d")
    cmd = s._build_ffmpeg_cmd("rtmp://live.example.com/app", "key1")
    assert "image2pipe" in cmd and "-i" in cmd and "-" in cmd
    assert cmd[-3:] == ["-f", "flv", "rtmp://live.example.com/app/key1"]


def test_headless_video_cmd_loops():
    from src.orchestrators.stream_orchestrator.headless_streamer import HeadlessStreamer
    s = HeadlessStreamer(source_type="video", source_path="data/clips/loop.mp4")
    cmd = s._build_ffmpeg_cmd("rtmp://live.example.com/app", "key1")
    assert "-re" in cmd and "-stream_loop" in cmd and "-1" in cmd
    assert "data/clips/loop.mp4" in cmd
    assert "-c:v" in cmd and "libx264" in cmd


def test_headless_image_cmd_loops_static():
    from src.orchestrators.stream_orchestrator.headless_streamer import HeadlessStreamer
    s = HeadlessStreamer(source_type="image", source_path="data/cover.png")
    cmd = s._build_ffmpeg_cmd("rtmp://live.example.com/app", "key1")
    assert "-loop" in cmd and "1" in cmd
    assert "data/cover.png" in cmd


def test_headless_invalid_source_type_raises():
    from src.orchestrators.stream_orchestrator.headless_streamer import HeadlessStreamer
    import pytest
    with pytest.raises(ValueError):
        HeadlessStreamer(source_type="unknown")


def test_headless_video_missing_path_raises_on_start():
    """video 模式 source_path 缺失/不存在 → start 抛 ValueError（不启动 ffmpeg）。"""
    from src.orchestrators.stream_orchestrator.headless_streamer import HeadlessStreamer
    import pytest
    s = HeadlessStreamer(source_type="video", source_path="")
    with pytest.raises(ValueError):
        s.start("rtmp://live.example.com/app", "key1")
    s2 = HeadlessStreamer(source_type="video", source_path="C:/nonexistent/loop.mp4")
    with pytest.raises(ValueError):
        s2.start("rtmp://live.example.com/app", "key1")


def test_headless_status_includes_source():
    from src.orchestrators.stream_orchestrator.headless_streamer import HeadlessStreamer
    s = HeadlessStreamer(source_type="image", source_path="data/cover.png")
    st = s.get_status()
    assert st["source_type"] == "image" and st["source_path"] == "data/cover.png"


def test_capabilities_from_registry():
    orch, _ = _make()
    caps = orch.capabilities()
    assert caps == registry.capabilities()
    assert "stream:start" in caps and "stream:app_register" in caps
    assert "obs:sources" in caps and "obs:open" in caps


def test_obs_sources_returns_manifest(monkeypatch):
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "obs:sources", "payload": {}}))
    assert r["ok"] is True
    sources = r["data"]["sources"]
    keys = {s["key"] for s in sources}
    assert keys == {"live2d", "danmaku_display", "danmaku_input", "subtitle_overlay"}
    live2d = next(s for s in sources if s["key"] == "live2d")
    assert "live2d.html" in live2d["url"] and live2d["name"]
    assert r["data"]["base"].startswith("http://")
    orch.stop()


def test_obs_open_known_source(monkeypatch):
    from src.orchestrators.stream_orchestrator import obs_sources
    called = {}
    monkeypatch.setattr(obs_sources, "open_url",
                        lambda url: called.update(url=url) or True)
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "obs:open",
                                 "payload": {"key": "字幕"}}))
    assert r["ok"] is True
    assert r["data"]["key"] == "subtitle_overlay"
    assert "/subtitle/" in r["data"]["url"]
    orch.stop()


def test_obs_open_unknown_source():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "obs:open",
                                 "payload": {"key": "不存在的源"}}))
    assert r["ok"] is False
    assert "unknown source" in r["error"]
    orch.stop()


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