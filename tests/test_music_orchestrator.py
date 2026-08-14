"""test_music_orchestrator.py — 音乐系统调度官单测（mock 播放器/歌曲库）"""
import asyncio

from src.orchestrators.music_orchestrator import registry
from src.orchestrators.music_orchestrator.music_orchestrator import MusicOrchestrator
from src.orchestrators.music_orchestrator.player import MusicPlayer
from src.orchestrators.music_orchestrator.song_manager import SongManager
from src.shared.event_bus import EventBus


class FakePlayer:
    def __init__(self):
        self.state = "stopped"
        self.volume = 0.8
        self.play_mode = "sequential"
        self.current = None
        self.playlist = []

    def play(self, song=None):
        self.state = "playing"
        self.current = song or {"title": "维塔利卡"}
        return True

    def pause(self):
        self.state = "paused"

    def resume(self):
        self.state = "playing"

    def stop(self):
        self.state = "stopped"

    def next(self):
        self.current = {"title": "下一曲"}
        return self.current

    def prev(self):
        self.current = {"title": "上一曲"}
        return self.current

    def set_volume(self, v):
        self.volume = v

    def get_volume(self):
        return self.volume

    def set_play_mode(self, m):
        self.play_mode = m

    def get_state(self):
        return self.state

    def get_current(self):
        return self.current

    def get_stats(self):
        return {"state": self.state, "volume": self.volume,
                "playlist_size": len(self.playlist), "current_index": 0,
                "play_mode": self.play_mode,
                "current_song": (self.current or {}).get("title")}


class FakeSongManager:
    def __init__(self):
        self.songs = {"s1": {"song_id": "s1", "title": "维塔利卡"}}
        self.queue = []

    def get_song(self, song_id):
        return self.songs.get(song_id)

    def list_songs(self):
        return list(self.songs.values())

    def search_songs(self, keyword="", artist="", tag=""):
        return [s for s in self.songs.values()
                if keyword in s.get("title", "")]

    def add_song(self, song_id, title, artist="", file_path="",
                 duration=0, tags=None):
        self.songs[song_id] = {"song_id": song_id, "title": title}
        return True

    def request_song(self, song_id, requester=""):
        if song_id not in self.songs:
            return False
        self.queue.append(song_id)
        return True

    def pop_request(self):
        if not self.queue:
            return None
        return self.songs[self.queue.pop(0)]

    def get_stats(self):
        return {"library_size": len(self.songs), "queue_size": len(self.queue)}


def _make():
    bus = EventBus()
    bus.reset()
    orch = MusicOrchestrator(event_bus=bus, player=FakePlayer(),
                              song_manager=FakeSongManager())
    orch.start()
    return orch, bus


def test_capabilities_from_registry():
    orch, _ = _make()
    caps = orch.capabilities()
    assert caps == registry.capabilities()
    assert "music:play" in caps and "music:request_song" in caps


def test_play_with_song():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "music:play",
                                 "payload": {"song": {"title": "BEGIN"}}}))
    assert r["ok"] is True and r["data"]["song"] == "BEGIN"


def test_play_from_queue():
    orch, _ = _make()
    # 先入队，再不带 song 播放取队首
    orch._song_manager.request_song("s1", "观众A")
    r = asyncio.run(orch.handle({"capability": "music:play", "payload": {}}))
    assert r["ok"] is True


def test_pause_resume_stop():
    orch, _ = _make()
    asyncio.run(orch.handle({"capability": "music:play", "payload": {}}))
    r = asyncio.run(orch.handle({"capability": "music:pause", "payload": {}}))
    assert r["data"]["state"] == "paused"
    r = asyncio.run(orch.handle({"capability": "music:resume", "payload": {}}))
    assert r["data"]["state"] == "playing"
    r = asyncio.run(orch.handle({"capability": "music:stop", "payload": {}}))
    assert r["data"]["state"] == "stopped"


def test_volume_and_mode():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "music:volume",
                                 "payload": {"volume": 0.3}}))
    assert r["data"]["volume"] == 0.3
    r = asyncio.run(orch.handle({"capability": "music:mode",
                                 "payload": {"mode": "shuffle"}}))
    assert r["data"]["mode"] == "shuffle"


def test_song_library_and_request():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "music:song_library",
                                 "payload": {}}))
    assert len(r["data"]["songs"]) == 1
    r = asyncio.run(orch.handle({"capability": "music:request_song",
                                 "payload": {"song_id": "s1", "requester": "A"}}))
    assert r["ok"] is True
    r = asyncio.run(orch.handle({"capability": "music:request_song",
                                 "payload": {"song_id": "不存在"}}))
    assert r["ok"] is False


def test_unknown_capability():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "music:unknown", "payload": {}}))
    assert r["ok"] is False


def test_health():
    orch, _ = _make()
    assert orch.health()["status"] == "ok"


def test_music_state_changed_event():
    bus = EventBus()
    bus.reset()
    seen = {}
    bus.subscribe("music:state_changed", lambda event, **kw: seen.update(kw))
    # 用真实 MusicPlayer：play 会发布 playing 事件（无音频文件时仅告警不崩溃）
    player = MusicPlayer(default_volume=0.8, event_bus=bus)
    orch = MusicOrchestrator(event_bus=bus, player=player,
                              song_manager=FakeSongManager())
    orch.start()
    asyncio.run(orch.handle({"capability": "music:play",
                             "payload": {"song": {"title": "BEGIN",
                                                  "file": "nonexistent.mp3"}}}))
    assert seen.get("state") == "playing"
    orch.stop()