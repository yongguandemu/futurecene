"""headless_streamer.py — 无头渲染 + ffmpeg 推流器（无人值守直播域）

管理 headless 渲染（逐帧 PNG 输入）+ ffmpeg 推流子进程。CPU 软编 720p@12fps，
适配无 GPU 环境。日志只记录目标域名与 key 长度，不落推流凭证明文。

# 模块内容清单 — headless_streamer

## 1. 模块身份标识
- 所属调度官：stream
- 能力名：stream:start / stream:stop / stream:alive

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| page_url | 否 | "http://127.0.0.1:8000/live2d-stream.html?paused=0" | str | 无头渲染页面地址 |
| width | 否 | 1280 | int，>0 | 输出分辨率宽 |
| height | 否 | 720 | int，>0 | 输出分辨率高 |
| fps | 否 | 12 | int，>0 | 推流帧率 |
| bitrate | 否 | "800k" | str | 视频码率（ffmpeg -b:v） |

## 3. 输入契约
- 输入格式：`start(server, key)` / `stop()` / `is_alive()` / `get_status()` / `close()`
- server：必填，str，RTMP 推流服务器地址（如 rtmp://live-send.bilivideo.com/live-bvc）
- key：必填，str，推流码（不落日志，仅记录长度）

## 4. 输出契约
- 成功：`start()` 返回 `True`；`stop()` 返回 `None`；`is_alive()` 返回 bool；`get_status()` 返回 `{"state": str, "ffmpeg_pid": int|None, "started_at": float|None, "page_url": str}`
- 失败：`start()` 已在推流 / 正在停止中返回 `False`；ffmpeg 启动异常时向上抛出
- 事件：无（状态由 get_status 轮询）

## 5. 依赖声明
- 外部服务：ffmpeg 可执行文件（缺失时 start 抛异常）、RTMP 服务器（B站等）
- 内部模块：无（纯 subprocess 管理）
- 预先配置：无

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 重复启动 | 已在推流中 / 正在停止中 | 返回 False，记录警告 |
| ffmpeg 启动失败 | Popen 异常（如 ffmpeg 未安装） | 置 idle 并向上抛出 |
| 终止超时 | terminate 等待 5s 超时 | kill 强制结束并等待 |
| 进程已退出 | terminate 时进程已自行结束 | 记录警告，忽略 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start | 是 | 构建 ffmpeg 命令并启动推流子进程 |
| stop | 是 | 终止推流子进程（terminate → kill 兜底），状态回 idle |
| close | 是 | 等价于 stop |

## 8. 领域状态说明
- 状态项：`_proc`（ffmpeg 子进程）、`_state`（idle/streaming/stopping）、`_started_at`（推流开始时间）
- 持久化：无
- 恢复：stop/close 后回到 idle；重启后重新 start
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