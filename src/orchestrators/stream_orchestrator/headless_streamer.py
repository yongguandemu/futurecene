"""headless_streamer.py — 无头渲染 + ffmpeg 推流器（无人值守直播域）

管理 headless 渲染（逐帧 PNG 输入）+ ffmpeg 推流子进程。CPU 软编 720p@12fps，
适配无 GPU 环境。日志只记录目标域名与 key 长度，不落推流凭证明文。

# 模块内容清单（8 项契约摘录）
- 所属调度官：stream
- 能力名：stream:start / stream:stop / stream:alive
- 配置契约：page_url / width(1280) / height(720) / fps(12) / bitrate(800k)
- 输入契约：start(server, key)；is_alive() -> bool
- 输出契约：bool；get_status() -> dict
- 生命周期：start()/stop()；领域状态：ffmpeg 子进程 + 推流标记
"""
import logging
import subprocess
import threading
import time
from typing import List, Optional

logger = logging.getLogger(__name__)

DEFAULT_PAGE_URL = "http://127.0.0.1:8000/live2d-stream.html?paused=0"


class HeadlessStreamer:
    """无头渲染 + 推流器：Popen 管理 ffmpeg 子进程，状态受线程锁保护。"""

    def __init__(self, page_url: str = DEFAULT_PAGE_URL,
                 width: int = 1280, height: int = 720,
                 fps: int = 12, bitrate: str = "800k"):
        self._page_url = page_url
        self._width = int(width)
        self._height = int(height)
        self._fps = int(fps)
        self._bitrate = bitrate
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._state = "idle"
        self._started_at: Optional[float] = None

    def _build_ffmpeg_cmd(self, server: str, key: str) -> List[str]:
        rtmp_url = server.rstrip("/") + "/" + key
        return ["ffmpeg", "-f", "image2pipe", "-framerate", str(self._fps),
                "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
                "-tune", "zerolatency", "-pix_fmt", "yuv420p",
                "-s", "{}x{}".format(self._width, self._height),
                "-b:v", self._bitrate, "-g", str(self._fps * 2),
                "-f", "flv", rtmp_url]

    def start(self, server: str, key: str) -> bool:
        with self._lock:
            if self._state == "stopping":
                logger.warning("[HeadlessStreamer] 正在停止中，拒绝启动")
                return False
            if self._proc is not None and self._proc.poll() is None:
                logger.warning("[HeadlessStreamer] 已在推流中，忽略重复 start")
                return False
            if self._proc is not None:
                logger.info("[HeadlessStreamer] 清理已退出的旧进程")
                self._proc = None
            cmd = self._build_ffmpeg_cmd(server, key)
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
            except Exception as e:
                self._proc = None
                self._state = "idle"
                logger.error("[HeadlessStreamer] 启动 ffmpeg 失败: %s", e)
                raise
            self._proc = proc
            self._state = "streaming"
            self._started_at = time.time()
            logger.info("[HeadlessStreamer] 开始推流 -> %s (key 长度=%d, %dx%d@%dfps)",
                        self._extract_host(server), len(key),
                        self._width, self._height, self._fps)
            return True

    @staticmethod
    def _extract_host(server: str) -> str:
        try:
            if "://" in server:
                return server.split("/")[2]
            return server
        except (AttributeError, IndexError, TypeError):
            return str(server)

    def stop(self) -> None:
        with self._lock:
            if self._state == "stopping":
                logger.warning("[HeadlessStreamer] 正在停止中，忽略重复 stop")
                return
            if self._proc is None:
                self._state = "idle"
                self._started_at = None
                return
            self._state = "stopping"
            self._started_at = None
            proc = self._proc
            self._proc = None
            try:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("[HeadlessStreamer] terminate 超时，强制 kill pid=%s", proc.pid)
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception as e:
                    logger.error("[HeadlessStreamer] kill 后等待退出失败 pid=%s: %s",
                                 proc.pid, e)
            except ProcessLookupError:
                logger.warning("[HeadlessStreamer] terminate 时进程已自行退出 pid=%s",
                               proc.pid)
            if proc.poll() is None:
                logger.warning("[HeadlessStreamer] 推流进程未完全退出 pid=%s，请人工确认",
                               proc.pid)
            else:
                logger.info("[HeadlessStreamer] 推流已停止 (pid=%s)", proc.pid)
            self._state = "idle"

    def close(self) -> None:
        self.stop()

    def is_alive(self) -> bool:
        with self._lock:
            proc = self._proc
        return proc is not None and proc.poll() is None

    def get_status(self) -> dict:
        with self._lock:
            proc = self._proc
            alive = proc is not None and proc.poll() is None
            if self._state == "stopping":
                return {"state": "stopping",
                        "ffmpeg_pid": proc.pid if alive else None,
                        "started_at": None, "page_url": self._page_url}
            return {"state": "streaming" if alive else "idle",
                    "ffmpeg_pid": proc.pid if alive else None,
                    "started_at": self._started_at if alive else None,
                    "page_url": self._page_url}