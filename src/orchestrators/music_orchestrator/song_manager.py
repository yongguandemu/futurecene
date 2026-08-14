"""song_manager.py — 歌曲管理器（音乐系统域）

管理歌曲库与点歌队列：添加/删除/搜索歌曲，点歌入队/出队，标签按心情匹配。

# 模块内容清单（8 项契约摘录）
- 所属调度官：music
- 能力名：music:request_song / song_library
- 配置契约：max_queue(50)
- 输入契约：add_song(song_id,title,...)；request_song(song_id, requester)
- 输出契约：request_song 入队成功返回 True；发布 music:song_requested
- 生命周期：无；领域状态：曲库 + 点歌队列（内存态）
"""
import time
import logging
import threading
from typing import Optional, Dict, Any, List

from src.shared.events import MUSIC_SONG_REQUESTED

logger = logging.getLogger(__name__)


class SongManager:
    """歌曲管理器 — 管理歌曲库与点歌队列。"""

    def __init__(self, library_path: Optional[str] = None, event_bus=None):
        self._library_path = library_path
        self._songs: Dict[str, Dict[str, Any]] = {}
        self._request_queue: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._max_queue = 50
        self._event_bus = event_bus
        logger.info("[SongManager] 初始化完成")

    def add_song(self, song_id: str, title: str, artist: str = "",
                 file_path: str = "", duration: float = 0,
                 tags: Optional[List[str]] = None) -> bool:
        with self._lock:
            if song_id in self._songs:
                return False
            self._songs[song_id] = {
                "song_id": song_id, "title": title, "artist": artist,
                "file_path": file_path, "duration": duration,
                "tags": tags or [], "play_count": 0, "added_at": time.time(),
            }
            logger.info("[SongManager] 添加歌曲: %s - %s", title, artist)
            return True

    def remove_song(self, song_id: str) -> bool:
        with self._lock:
            return self._songs.pop(song_id, None) is not None

    def get_song(self, song_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._songs.get(song_id)

    def search_songs(self, keyword: str = "", artist: str = "",
                     tag: str = "") -> List[Dict[str, Any]]:
        with self._lock:
            results = list(self._songs.values())
            if keyword:
                kw = keyword.lower()
                results = [s for s in results if kw in s["title"].lower()]
            if artist:
                ar = artist.lower()
                results = [s for s in results if ar in s["artist"].lower()]
            if tag:
                results = [s for s in results if tag in s.get("tags", [])]
            return results

    def list_songs(self, sort_by: str = "title") -> List[Dict[str, Any]]:
        with self._lock:
            songs = list(self._songs.values())
            if sort_by in ("title", "artist", "play_count", "added_at"):
                songs.sort(key=lambda s: s.get(sort_by, 0),
                           reverse=(sort_by == "play_count"))
            return songs

    def request_song(self, song_id: str, requester: str = "") -> bool:
        with self._lock:
            if song_id not in self._songs:
                return False
            if len(self._request_queue) >= self._max_queue:
                logger.warning("[SongManager] 点歌队列已满")
                return False
            self._request_queue.append({
                "song_id": song_id, "requester": requester,
                "timestamp": time.time(),
            })
            logger.info("[SongManager] 点歌: %s (by %s)", song_id, requester)
        self._publish_request(song_id, requester)
        return True

    def _publish_request(self, song_id: str, requester: str):
        if self._event_bus:
            try:
                self._event_bus.publish(MUSIC_SONG_REQUESTED, song_id=song_id,
                                        requester=requester)
            except Exception as e:
                logger.warning("[SongManager] 发布点歌事件失败: %s", e)

    def get_request_queue(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._request_queue)

    def pop_request(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._request_queue:
                req = self._request_queue.pop(0)
                song = self._songs.get(req["song_id"])
                if song:
                    song["play_count"] += 1
                return req
            return None

    def increment_play_count(self, song_id: str) -> None:
        with self._lock:
            song = self._songs.get(song_id)
            if song:
                song["play_count"] += 1

    def by_mood(self, mood: str) -> List[Dict[str, Any]]:
        return self.search_songs(tag=mood)

    def get_songs(self, sort_by: str = "title") -> List[Dict[str, Any]]:
        return self.list_songs(sort_by)

    def search(self, keyword: str = "", artist: str = "",
               tag: str = "") -> List[Dict[str, Any]]:
        return self.search_songs(keyword, artist, tag)

    def get_random(self, count: int = 1, tag: str = "") -> List[Dict[str, Any]]:
        import random as _random
        with self._lock:
            pool = list(self._songs.values())
        if tag:
            pool = [s for s in pool if tag in s.get("tags", [])]
        if not pool:
            return []
        count = min(count, len(pool))
        return _random.sample(pool, count)

    def get_top_played(self, count: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            songs = list(self._songs.values())
        songs.sort(key=lambda s: s.get("play_count", 0), reverse=True)
        return songs[:count]

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {"total_songs": len(self._songs),
                    "queue_size": len(self._request_queue),
                    "total_plays": sum(s["play_count"] for s in self._songs.values())}