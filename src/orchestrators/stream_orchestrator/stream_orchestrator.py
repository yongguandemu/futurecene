"""stream_orchestrator.py — 无人值守直播调度官主类（规格书 P2）

能力：stream:start / stop / state / fetch_code / launch_app / app_terminate /
      app_list / app_register，以及 OBS 浏览器源 obs:sources / obs:open。
职责边界：编排无人值守直播生命周期（取推流码 → 推流 → 心跳保活 → 停播），
外加外部应用启动器与 OBS 直播叠加源登记/打开。状态受线程锁保护，
状态变更发布 stream:state_changed。

# 模块内容清单（8 项契约）
1. 模块身份标识：stream · StreamOrchestrator · 能力 stream:start/stop/state/fetch_code/launch_app/app_terminate/app_list/app_register + obs:sources/obs:open
2. 配置契约：config.room_id、config.identity_code（推流码刷新）；心跳/重试常量
3. 输入契约：handle(command) 指令字典（capability + payload：name/path/args/cwd/env 或 obs 源 key 等）
4. 输出契约：{ok, data, error} 响应字典；发布 stream:state_changed 事件；obs:sources 返回源清单，obs:open 返回打开结果
5. 依赖声明：logging、threading、time、typing、registry、obs_sources、HeadlessStreamer、StreamCodeRefresher、AgentLauncher、src.shared.events
6. 错误定义：获取推流码/启动推流失败进入 failed 状态并返回 error；心跳检测推流进程意外退出
7. 生命周期方法：start()/stop()/health()；内部 _start_heartbeat()/_stop_heartbeat()
8. 领域状态说明：_state 直播状态机（idle/starting/live/stopping/failed）、_last_error、_started_at、心跳线程与失败计数
"""
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from src.orchestrators.stream_orchestrator import registry
from src.orchestrators.stream_orchestrator import obs_sources
from src.orchestrators.stream_orchestrator.agent_launcher import AgentLauncher
from src.orchestrators.stream_orchestrator.headless_streamer import (
    DEFAULT_PAGE_URL,
    HeadlessStreamer,
    SOURCE_LIVE2D,
)
from src.orchestrators.stream_orchestrator.stream_code_refresher import (
    StreamCodeRefresher,
)
from src.shared.events import STREAM_STATE_CHANGED

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 30.0
RETRY_MAX = 3
RETRY_INTERVAL = 2.0
HEARTBEAT_FAIL_THRESHOLD = 2


class StreamOrchestrator:
    """无人值守直播调度官。"""

    name = "stream"

    def __init__(self, event_bus, config: Optional[Dict[str, Any]] = None,
                 refresher: Optional[StreamCodeRefresher] = None,
                 streamer: Optional[HeadlessStreamer] = None,
                 launcher: Optional[AgentLauncher] = None):
        cfg = config or {}
        self._config = cfg
        self._event_bus = event_bus
        self._refresher = refresher or StreamCodeRefresher(
            room_id=cfg.get("room_id", 0),
            identity_code=cfg.get("identity_code", ""))
        self._streamer = streamer or HeadlessStreamer(
            page_url=cfg.get("page_url", DEFAULT_PAGE_URL),
            width=cfg.get("width", 1280),
            height=cfg.get("height", 720),
            fps=cfg.get("fps", 12),
            bitrate=cfg.get("bitrate", "800k"),
            source_type=cfg.get("source_type", SOURCE_LIVE2D),
            source_path=cfg.get("source_path", ""))
        self._launcher = launcher or AgentLauncher()
        self._lock = threading.Lock()
        self._state = "idle"
        self._last_error: Optional[str] = None
        self._started_at: Optional[float] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop = threading.Event()
        self._alive_fail_count = 0
        self._started = False
        registry.bind(self.handle)

    def capabilities(self) -> List[str]:
        return registry.capabilities()

    def start(self) -> None:
        self._started = True
        logger.info("[StreamOrchestrator] 已启动")

    async def handle(self, command: Dict[str, Any]) -> Dict[str, Any]:
        capability = command.get("capability", "")
        payload = command.get("payload", {})
        if capability == "stream:start":
            return self._start_live()
        if capability == "stream:stop":
            return self._stop_live()
        if capability == "stream:state":
            return {"ok": True, "data": self.get_status(), "error": None}
        if capability == "stream:fetch_code":
            return self._fetch_code()
        if capability == "stream:launch_app":
            return self._launch_app(payload)
        if capability == "stream:app_terminate":
            return self._app_terminate(payload)
        if capability == "stream:app_list":
            return {"ok": True, "data": self._launcher.list_launched(),
                    "error": None}
        if capability == "stream:app_register":
            return self._app_register(payload)
        if capability == "obs:sources":
            return {"ok": True,
                    "data": {"sources": obs_sources.manifest(),
                             "base": obs_sources.DEFAULT_BASE},
                    "error": None}
        if capability == "obs:open":
            return obs_sources.open_source(payload.get("key", ""))
        return {"ok": False, "data": {}, "error": f"unknown capability: {capability}"}

    def health(self) -> Dict[str, Any]:
        return {"status": "ok" if self._started else "down",
                "detail": f"state={self._state}"}

    def stop(self) -> None:
        self._stop_heartbeat()
        try:
            self._streamer.stop()
        except Exception:
            pass
        try:
            self._launcher.close()
        except Exception:
            pass
        self._started = False

    # ---------- 内部实现 ----------

    def _ensure_state(self, *allowed: str) -> bool:
        if self._state not in allowed:
            logger.warning("[Stream] 状态守卫拒绝：当前 %s，期望 %s",
                           self._state, "/".join(allowed))
            return False
        return True

    def _safe_host(self, server: str) -> str:
        extract = getattr(self._streamer, "_extract_host", None)
        if callable(extract):
            try:
                host = extract(server)
                if isinstance(host, str) and host:
                    return host
            except Exception:
                pass
        try:
            return server.split("//", 1)[1].split("/", 1)[0]
        except Exception:
            return "rtmp 地址（域名）"

    def _start_live(self) -> Dict[str, Any]:
        with self._lock:
            if self._state in ("starting", "live", "stopping"):
                logger.warning("[Stream] 当前状态 %s，拒绝重复开播", self._state)
                return {"ok": False, "data": {}, "error": "状态不允许开播"}
            self._state = "starting"
            self._last_error = None
            self._publish_state()
        try:
            server, key = self._manual_stream_code()
            if not server or not key:
                server, key = self._refresher.fetch_stream_code()
        except Exception as e:
            logger.error("[Stream] 获取推流码失败: %s", e)
            self._fail("获取推流码失败: {}".format(e))
            return {"ok": False, "data": {}, "error": str(e)}
        with self._lock:
            if not self._ensure_state("starting"):
                return {"ok": False, "data": {}, "error": "状态已变更"}
        ok = False
        last_err = ""
        for attempt in range(1, RETRY_MAX + 1):
            try:
                ok = bool(self._streamer.start(server, key))
            except Exception as e:
                last_err = str(e)
                logger.warning("[Stream] 启动推流第 %d/%d 次失败: %s",
                               attempt, RETRY_MAX, last_err)
            else:
                if ok:
                    break
                last_err = "streamer.start 返回 False"
            if attempt < RETRY_MAX:
                time.sleep(RETRY_INTERVAL)
        if not ok:
            self._fail("启动推流失败: {}".format(last_err or "未知原因"))
            return {"ok": False, "data": {}, "error": last_err}
        with self._lock:
            if not self._ensure_state("starting"):
                try:
                    self._streamer.stop()
                except Exception:
                    pass
                return {"ok": False, "data": {}, "error": "状态已变更"}
            self._state = "live"
            self._started_at = time.time()
            self._alive_fail_count = 0
            self._publish_state()
        host = self._safe_host(server)
        logger.info("[Stream] 开播成功 -> %s (key 长度=%d)", host, len(key))
        self._start_heartbeat()
        return {"ok": True,
                "data": {"state": "live", "host": host, "key_len": len(key)},
                "error": None}

    def _stop_live(self) -> Dict[str, Any]:
        with self._lock:
            if self._state in ("idle", "stopping", "starting"):
                logger.info("[Stream] 当前状态 %s，无需停播", self._state)
                return {"ok": False, "data": {}, "error": "状态无需停播"}
            self._state = "stopping"
            self._publish_state()
        self._stop_heartbeat()
        try:
            self._streamer.stop()
        except Exception as e:
            logger.warning("[Stream] 停播时关闭推流进程异常: %s", e)
            with self._lock:
                self._last_error = "停播清理异常: {}".format(e)
        with self._lock:
            self._state = "idle"
            self._started_at = None
            self._publish_state()
        logger.info("[Stream] 已停播")
        return {"ok": True, "data": {"state": "idle"}, "error": None}

    def _fail(self, reason: str) -> None:
        logger.error("[Stream] 直播失败: %s", reason)
        with self._lock:
            if not self._ensure_state("starting", "live"):
                logger.info("[Stream] 当前状态 %s，忽略失败兜底", self._state)
                return
            self._state = "failed"
            self._last_error = reason
            self._started_at = None
            self._alive_fail_count = 0
            self._publish_state()
        self._stop_heartbeat()
        try:
            self._streamer.stop()
        except Exception as e:
            logger.warning("[Stream] 失败兜底关闭推流进程异常: %s", e)

    def _manual_stream_code(self):
        """手动推流码（config stream.rtmp_server + rtmp_key）：开播前直接填入即可，
        无需 B站 Cookie / 直播间；未配置返回 (None, None) 走自动刷新。"""
        server = str(self._config.get("rtmp_server") or "")
        key = str(self._config.get("rtmp_key") or "")
        if server and key:
            logger.info("[Stream] 使用手动推流码 -> %s (key 长度=%d)",
                        self._safe_host(server), len(key))
        return (server, key) if server and key else (None, None)

    def _start_heartbeat(self) -> None:
        self._heartbeat_stop.clear()
        t = threading.Thread(target=self._heartbeat_loop, daemon=True,
                             name="StreamHeartbeat")
        self._heartbeat_thread = t
        t.start()

    def _stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        t = self._heartbeat_thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=HEARTBEAT_INTERVAL + 1.0)
        self._heartbeat_thread = None

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(HEARTBEAT_INTERVAL):
            with self._lock:
                if self._state != "live":
                    return
            try:
                alive = self._streamer.is_alive()
            except Exception:
                alive = False
            if alive:
                self._alive_fail_count = 0
                continue
            self._alive_fail_count += 1
            if self._alive_fail_count < HEARTBEAT_FAIL_THRESHOLD:
                continue
            logger.error("[Stream] 推流进程意外退出")
            self._fail("推流进程意外退出")
            return

    def _fetch_code(self) -> Dict[str, Any]:
        try:
            server, key = self._refresher.fetch_stream_code()
            return {"ok": True,
                    "data": {"host": self._safe_host(server), "key_len": len(key)},
                    "error": None}
        except Exception as e:
            return {"ok": False, "data": {}, "error": str(e)}

    def _launch_app(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ok = self._launcher.launch(payload.get("name", ""),
                                   path=payload.get("path"),
                                   args=payload.get("args"),
                                   cwd=payload.get("cwd"),
                                   env=payload.get("env"))
        return {"ok": ok, "data": {"pid": self._launcher.get_pid(payload.get("name", ""))},
                "error": None if ok else "启动失败"}

    def _app_terminate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ok = self._launcher.terminate(payload.get("name", ""))
        return {"ok": ok, "data": {}, "error": None}

    def _app_register(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._launcher.register_template(payload.get("name", ""),
                                         payload.get("path", ""),
                                         args=payload.get("args"),
                                         cwd=payload.get("cwd"),
                                         env=payload.get("env"),
                                         auto_start=bool(payload.get("auto_start")))
        return {"ok": True, "data": {}, "error": None}

    def _publish_state(self) -> None:
        if self._event_bus is None:
            return
        try:
            self._event_bus.publish(STREAM_STATE_CHANGED, state=self._state,
                                    error=self._last_error)
        except Exception as e:
            logger.warning("[Stream] 发布状态事件失败: %s", e)

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            try:
                streamer_status = self._streamer.get_status()
            except Exception as e:
                streamer_status = {"error": str(e)}
            return {"state": self._state, "last_error": self._last_error,
                    "streamer": streamer_status, "started_at": self._started_at}