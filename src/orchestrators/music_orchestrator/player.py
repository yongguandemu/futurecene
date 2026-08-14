"""player.py — 音乐播放器（音乐系统域）

播放控制 + 播放队列 + 音量调节。支持 ffplay 音频后端（播放本地翻唱文件），
经 EventBus 发布 music:state_changed（VoiceBridge 据此在播放时抑制 TTS 发声）。

# 模块内容清单（8 项契约摘录）
- 所属调度官：music
- 能力名：music:play / pause / resume / stop / next / prev / volume / mode
- 配置契约：default_volume(0.8) / 播放模式(sequential/shuffle/repeat_one)
- 输入契约：play(song)、pause()、next() 等
- 输出契约：get_state()/get_stats()；发布 music:state_changed
- 生命周期：start()/stop()；领域状态：播放状态机 + 播放列表
"""
import os
import subprocess
import time
import logging
import threading
from pathlib import Path
from typing import Optional, Dict, Any, List

from src.shared.events import MUSIC_STATE_CHANGED

logger = logging.getLogger(__name__)


class MusicPlayer:
    """音乐播放器 — 控制播放、暂停、切歌和音量。"""

    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"

    SEQUENTIAL = "sequential"
    SHUFFLE = "shuffle"
    REPEAT_ONE = "repeat_one"

    def __init__(self, default_volume: float = 0.8, event_bus=None,
                 ffplay_path: Optional[str] = None):
        self._state = self.STOPPED
        self._volume = default_volume
        self._playlist: List[Dict[str, Any]] = []
        self._current_index: int = -1
        self._play_mode = self.SEQUENTIAL
        self._current_song: Optional[Dict[str, Any]] = None
        self._play_started_at: float = 0
        self._pause_time: float = 0
        self._lock = threading.Lock()
        self._handlers: List = []
        self._event_bus = event_bus
        self._ffplay = ffplay_path or self._find_ffplay()
        self._proc: Optional[subprocess.Popen] = None
        logger.info("[MusicPlayer] 初始化完成 (volume=%.0f%%, ffplay=%s)",
                    default_volume * 100, self._ffplay or "未找到")

    # ------------------------------------------------------------------
    # 音频后端
    # ------------------------------------------------------------------

    @staticmethod
    def _find_ffplay() -> Optional[str]:
        candidates = [
            os.environ.get("LUMI_FFMPEG", "").replace("ffmpeg.exe", "ffplay.exe"),
            str(Path(__file__).parent.parent / "tools" / "ffmpeg" / "ffplay.exe"),
            str(Path.home() / "LumiProjectTools" / "ffmpeg" / "ffplay.exe"),
            str(Path.home() / "AppData" / "Local" / "LumiTools" / "ffmpeg" / "ffplay.exe"),
        ]
        for c in candidates:
            if c and os.path.exists(c):
                return c
        try:
            return __import__("shutil").which("ffplay")
        except Exception:
            return None

    def _start_audio(self, song: Optional[Dict[str, Any]]) -> None:
        self._stop_audio()
        file = (song or {}).get("file")
        if not file or not os.path.exists(file):
            if song:
                logger.warning("[MusicPlayer] 音频文件不存在: %s", file)
            return
        if not self._ffplay:
            logger.warning("[MusicPlayer] 未找到 ffplay，无法输出音频")
            return
        try:
            vol = max(0, min(100, int(self._volume * 100)))
            self._proc = subprocess.Popen(
                [self._ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet",
                 "-volume", str(vol), str(file)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            logger.info("[MusicPlayer] ffplay 播放: %s (pid=%d)",
                        file, self._proc.pid)
        except Exception as e:
            logger.warning("[MusicPlayer] ffplay 启动失败: %s", e)
            self._proc = None

    def _stop_audio(self) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _publish(self, state: str, song: Optional[Dict[str, Any]]) -> None:
        if self._event_bus:
            try:
                self._event_bus.publish(MUSIC_STATE_CHANGED, state=state,
                                        song=(song or {}).get("title", ""))
            except Exception as e:
                logger.warning("[MusicPlayer] 发布事件失败: %s", e)

    # ------------------------------------------------------------------
    # 播放控制
    # ------------------------------------------------------------------

    def add_to_playlist(self, song: Dict[str, Any]) -> None:
        with self._lock:
            self._playlist.append(song)

    def set_playlist(self, songs: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._playlist = list(songs)
            self._current_index = -1

    def play(self, song: Optional[Dict[str, Any]] = None) -> bool:
        with self._lock:
            if song is not None:
                self._current_song = song
                self._current_index = -1
            elif self._current_song is None and self._playlist:
                self._current_index = 0
                self._current_song = self._playlist[0]
            elif self._current_song is None:
                logger.warning("[MusicPlayer] 播放列表为空")
                return False
            self._state = self.PLAYING
            self._play_started_at = time.time()
            self._pause_time = 0
            logger.info("[MusicPlayer] 播放: %s",
                        self._current_song.get("title", "unknown"))
            self._start_audio(self._current_song)
        self._publish("playing", self._current_song)
        self._notify_handlers("play", self._current_song)
        return True

    def pause(self) -> None:
        with self._lock:
            if self._state == self.PLAYING:
                self._state = self.PAUSED
                self._pause_time = time.time()
                self._stop_audio()
        self._publish("paused", self._current_song)
        self._notify_handlers("pause", self._current_song)

    def resume(self) -> None:
        with self._lock:
            if self._state == self.PAUSED:
                self._state = self.PLAYING
                self._start_audio(self._current_song)
        self._publish("playing", self._current_song)
        self._notify_handlers("resume", self._current_song)

    def stop(self) -> None:
        with self._lock:
            self._state = self.STOPPED
            self._current_song = None
            self._play_started_at = 0
            self._stop_audio()
        self._publish("stopped", None)
        self._notify_handlers("stop", None)

    def next(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self._playlist:
                return None
            if self._play_mode == self.SHUFFLE:
                import random
                self._current_index = random.randint(0, len(self._playlist) - 1)
            else:
                self._current_index = (self._current_index + 1) % len(self._playlist)
            self._current_song = self._playlist[self._current_index]
            self._state = self.PLAYING
            self._play_started_at = time.time()
            self._start_audio(self._current_song)
        self._publish("playing", self._current_song)
        self._notify_handlers("next", self._current_song)
        return self._current_song

    def prev(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self._playlist:
                return None
            self._current_index = (self._current_index - 1) % len(self._playlist)
            self._current_song = self._playlist[self._current_index]
            self._state = self.PLAYING
            self._play_started_at = time.time()
            self._start_audio(self._current_song)
        self._publish("playing", self._current_song)
        self._notify_handlers("prev", self._current_song)
        return self._current_song

    def set_volume(self, volume: float) -> None:
        with self._lock:
            self._volume = max(0.0, min(1.0, volume))

    def get_volume(self) -> float:
        return self._volume

    def set_play_mode(self, mode: str) -> None:
        with self._lock:
            self._play_mode = mode

    def get_current(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._current_song

    def get_state(self) -> str:
        with self._lock:
            return self._state

    def get_position(self) -> float:
        with self._lock:
            if self._state != self.PLAYING:
                return 0
            return time.time() - self._play_started_at

    def register_handler(self, handler) -> None:
        self._handlers.append(handler)

    def _notify_handlers(self, event: str, song: Optional[Dict]) -> None:
        for handler in self._handlers:
            try:
                handler(event, song)
            except Exception as e:
                logger.warning("[MusicPlayer] 处理器异常: %s", e)

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "volume": round(self._volume, 2),
                "playlist_size": len(self._playlist),
                "current_index": self._current_index,
                "play_mode": self._play_mode,
                "current_song": (
                    self._current_song.get("title") if self._current_song else None
                ),
            }