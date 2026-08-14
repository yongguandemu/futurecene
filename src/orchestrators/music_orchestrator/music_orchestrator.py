"""music_orchestrator.py — 音乐系统调度官主类（规格书 P2）

能力：music:play / pause / resume / stop / next / prev / volume / mode /
      state / stats / playlist / request_song / song_search / song_add /
      song_library。
职责边界：点歌事件经 EventBus 发布 music:song_requested；播放状态变更发布
music:state_changed（供 VoiceBridge 互斥）。音频输出经 ffplay 后端。

# 模块内容清单（8 项契约）
1. 模块身份标识：music 调度官 · music_orchestrator · 能力 music:play/pause/resume/stop/next/prev/volume/mode/state/stats/playlist/request_song/song_search/song_add/song_library
2. 配置契约：default_volume(0.8)
3. 输入契约：handle(command) 接收 {"capability": "music:*", "payload": {"song","song_id","volume","mode","songs","keyword","artist","tag","title","file_path","duration","tags","requester"}}
4. 输出契约：返回 {"ok": bool, "data": {...}, "error": str|null}；播放状态经 MusicPlayer 发布 music:state_changed，点歌经 SongManager 发布 music:song_requested
5. 依赖声明：registry、player、song_manager
6. 错误定义：无可用歌曲/播放失败/点歌失败返回 {"ok": false, "error": ...}
7. 生命周期方法：start()、stop()（停止播放）、health()、handle() 能力分发
8. 领域状态说明：_player（MusicPlayer）、_song_manager（SongManager）、_started
"""
import logging
from typing import Any, Dict, List, Optional

from src.orchestrators.music_orchestrator import registry
from src.orchestrators.music_orchestrator.player import MusicPlayer
from src.orchestrators.music_orchestrator.song_manager import SongManager

logger = logging.getLogger(__name__)


class MusicOrchestrator:
    """音乐系统调度官。"""

    name = "music"

    def __init__(self, event_bus, config: Optional[Dict[str, Any]] = None,
                 player: Optional[MusicPlayer] = None,
                 song_manager: Optional[SongManager] = None):
        self._event_bus = event_bus
        self._config = config or {}
        self._player = player or MusicPlayer(
            default_volume=self._config.get("default_volume", 0.8),
            event_bus=event_bus)
        self._song_manager = song_manager or SongManager(event_bus=event_bus)
        self._started = False
        registry.bind(self.handle)

    def capabilities(self) -> List[str]:
        return registry.capabilities()

    def start(self) -> None:
        self._started = True
        logger.info("[MusicOrchestrator] 已启动")

    async def handle(self, command: Dict[str, Any]) -> Dict[str, Any]:
        capability = command.get("capability", "")
        payload = command.get("payload", {})
        if capability == "music:play":
            return self._play(payload)
        if capability == "music:pause":
            self._player.pause()
            return self._ok({"state": self._player.get_state()})
        if capability == "music:resume":
            self._player.resume()
            return self._ok({"state": self._player.get_state()})
        if capability == "music:stop":
            self._player.stop()
            return self._ok({"state": self._player.get_state()})
        if capability == "music:next":
            song = self._player.next()
            return self._ok({"song": (song or {}).get("title") if song else None})
        if capability == "music:prev":
            song = self._player.prev()
            return self._ok({"song": (song or {}).get("title") if song else None})
        if capability == "music:volume":
            return self._volume(payload)
        if capability == "music:mode":
            return self._mode(payload)
        if capability == "music:state":
            return self._ok(self._player.get_stats())
        if capability == "music:stats":
            return self._ok({"player": self._player.get_stats(),
                             "library": self._song_manager.get_stats()})
        if capability == "music:playlist":
            return self._playlist(payload)
        if capability == "music:request_song":
            return self._request_song(payload)
        if capability == "music:song_search":
            return self._song_search(payload)
        if capability == "music:song_add":
            return self._song_add(payload)
        if capability == "music:song_library":
            return self._ok({"songs": self._song_manager.list_songs()})
        return {"ok": False, "data": {}, "error": f"unknown capability: {capability}"}

    def health(self) -> Dict[str, Any]:
        return {"status": "ok" if self._started else "down",
                "detail": f"state={self._player.get_state()}"}

    def stop(self) -> None:
        self._player.stop()
        self._started = False

    # ---------- 内部实现 ----------

    def _ok(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, "data": data, "error": None}

    def _play(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        song = payload.get("song")
        song_id = payload.get("song_id")
        if song is None and song_id:
            song = self._song_manager.get_song(song_id)
        if song is None:
            # 从点歌队列取队首
            req = self._song_manager.pop_request()
            if req is None or not self._player.play():
                return {"ok": False, "data": {}, "error": "无可用歌曲"}
            return self._ok({"song": self._player.get_current().get("title")})
        ok = self._player.play(song)
        return self._ok({"song": (song or {}).get("title")}) if ok else \
            {"ok": False, "data": {}, "error": "播放失败"}

    def _volume(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        vol = payload.get("volume")
        if vol is not None:
            self._player.set_volume(float(vol))
        return self._ok({"volume": self._player.get_volume()})

    def _mode(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        mode = payload.get("mode")
        if mode:
            self._player.set_play_mode(mode)
        return self._ok({"mode": self._player.get_stats()["play_mode"]})

    def _playlist(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        songs = payload.get("songs")
        if songs is not None:
            self._player.set_playlist(songs)
        return self._ok({"playlist": self._player.get_stats()["playlist_size"]})

    def _request_song(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ok = self._song_manager.request_song(payload.get("song_id", ""),
                                             payload.get("requester", ""))
        return self._ok({"queued": ok}) if ok else \
            {"ok": False, "data": {}, "error": "点歌失败（不存在或队列满）"}

    def _song_search(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        songs = self._song_manager.search_songs(
            keyword=payload.get("keyword", ""), artist=payload.get("artist", ""),
            tag=payload.get("tag", ""))
        return self._ok({"songs": songs})

    def _song_add(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ok = self._song_manager.add_song(
            payload.get("song_id", ""), payload.get("title", ""),
            artist=payload.get("artist", ""), file_path=payload.get("file_path", ""),
            duration=payload.get("duration", 0), tags=payload.get("tags"))
        return self._ok({"added": ok})