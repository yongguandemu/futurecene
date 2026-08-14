"""agent_launcher.py — 应用/代理启动器（无人值守直播域）

启动和管理外部应用程序（OBS、VTS、游戏、直播工具等）的生命周期：
注册模板、启动/终止/重启、进程监控、自启列表。

# 模块内容清单（8 项契约摘录）
- 所属调度官：stream
- 能力名：stream:launch_app / app_terminate / app_status
- 配置契约：模板（name → path/args/cwd/env/auto_start）
- 输入契约：launch(name, path, args, cwd, env) -> bool
- 输出契约：bool；list_launched() -> list
- 生命周期：launch()/terminate()/close()；领域状态：模板表 + 已启动进程
"""
import os
import time
import logging
import threading
import subprocess
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


class AgentLauncher:
    """应用/代理启动器 — 管理外部应用程序的生命周期。"""

    def __init__(self):
        self._templates: Dict[str, Dict] = {}
        self._launched: Dict[str, Dict] = {}
        self._auto_start: List[str] = []
        self._lock = threading.RLock()
        logger.info("[AgentLauncher] 初始化完成")

    def register_template(self, name: str, path: str,
                          args: Optional[List[str]] = None,
                          cwd: Optional[str] = None,
                          env: Optional[Dict] = None,
                          auto_start: bool = False):
        with self._lock:
            self._templates[name] = {"name": name, "path": path,
                                     "args": args or [], "cwd": cwd,
                                     "env": env, "auto_start": auto_start,
                                     "registered_at": time.time()}
            if auto_start and name not in self._auto_start:
                self._auto_start.append(name)
            logger.info("[AgentLauncher] 已注册模板: %s -> %s", name, path)

    def unregister_template(self, name: str):
        with self._lock:
            self._templates.pop(name, None)
            if name in self._auto_start:
                self._auto_start.remove(name)

    def list_templates(self) -> List[Dict]:
        with self._lock:
            return [t.copy() for t in self._templates.values()]

    def launch(self, name: str, path: Optional[str] = None,
               args: Optional[List[str]] = None, cwd: Optional[str] = None,
               env: Optional[Dict] = None) -> bool:
        with self._lock:
            if path is None and name in self._templates:
                tpl = self._templates[name]
                path = tpl["path"]
                args = args or tpl["args"]
                cwd = cwd or tpl["cwd"]
                env = env or tpl["env"]
            if not path:
                logger.error("[AgentLauncher] 启动失败 %s: 未指定路径", name)
                return False
            if name in self._launched and self.is_running(name):
                logger.warning("[AgentLauncher] %s 已在运行，先终止旧进程", name)
                self.terminate(name)
            try:
                proc = subprocess.Popen([path] + (args or []),
                                        cwd=cwd,
                                        env={**os.environ, **env} if env else None,
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
                self._launched[name] = {"process": proc, "pid": proc.pid,
                                        "path": path, "args": args or [],
                                        "started_at": time.time()}
                logger.info("[AgentLauncher] 已启动 %s (PID=%s)", name, proc.pid)
                return True
            except FileNotFoundError:
                logger.error("[AgentLauncher] 启动失败 %s: 文件不存在 %s", name, path)
                return False
            except Exception as e:
                logger.error("[AgentLauncher] 启动失败 %s: %s", name, e)
                return False

    def terminate(self, name: str, timeout: float = 5.0) -> bool:
        with self._lock:
            app = self._launched.get(name)
            if not app:
                return False
        try:
            proc = app["process"]
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            with self._lock:
                self._launched.pop(name, None)
            logger.info("[AgentLauncher] 已终止 %s", name)
            return True
        except Exception as e:
            logger.error("[AgentLauncher] 终止失败 %s: %s", name, e)
            return False

    def restart(self, name: str) -> bool:
        with self._lock:
            if name not in self._templates and name not in self._launched:
                return False
        self.terminate(name)
        time.sleep(0.5)
        return self.launch(name)

    def is_running(self, name: str) -> bool:
        with self._lock:
            app = self._launched.get(name)
            return bool(app and app["process"].poll() is None)

    def get_pid(self, name: str) -> Optional[int]:
        with self._lock:
            app = self._launched.get(name)
            return app["pid"] if app else None

    def list_launched(self) -> List[Dict]:
        with self._lock:
            return [{"name": name, "pid": info["pid"], "path": info["path"],
                     "running": self.is_running(name),
                     "uptime": time.time() - info.get("started_at", time.time())}
                    for name, info in self._launched.items()]

    def get_app_info(self, name: str) -> Optional[Dict]:
        with self._lock:
            info = self._launched.get(name)
            if not info:
                return None
            return {"name": name, "pid": info["pid"], "path": info["path"],
                    "args": info.get("args", []), "running": self.is_running(name),
                    "started_at": info.get("started_at"),
                    "uptime": time.time() - info.get("started_at", time.time())}

    def start_auto_apps(self):
        for name in list(self._auto_start):
            if not self.is_running(name):
                self.launch(name)

    def terminate_all(self):
        with self._lock:
            names = list(self._launched.keys())
        for name in names:
            self.terminate(name)

    def cleanup(self):
        with self._lock:
            dead = [name for name in self._launched if not self.is_running(name)]
            for name in dead:
                info = self._launched.pop(name)
                logger.info("[AgentLauncher] 清理已退出进程: %s (PID=%s)",
                            name, info["pid"])

    def close(self):
        try:
            self.terminate_all()
        except Exception:
            pass