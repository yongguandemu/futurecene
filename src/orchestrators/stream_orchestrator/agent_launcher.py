"""agent_launcher.py — 应用/代理启动器（无人值守直播域）

启动和管理外部应用程序（OBS、VTS、游戏、直播工具等）的生命周期：
注册模板、启动/终止/重启、进程监控、自启列表。

# 模块内容清单 — agent_launcher

## 1. 模块身份标识
- 所属调度官：stream
- 能力名：stream:launch_app / stream:app_terminate / stream:app_status

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| 应用模板 | 否 | 无 | dict | register_template(name, path, args, cwd, env, auto_start) 注册；name 为唯一标识 |
| auto_start | 否 | False | bool | 模板是否进入自启列表（start_auto_apps 时启动） |

## 3. 输入契约
- 输入格式：`launch(name, path, args, cwd, env)` / `terminate(name, timeout)` / `restart(name)` / `is_running(name)` / `get_pid(name)` / `list_launched()` / `get_app_info(name)` / `start_auto_apps()` / `terminate_all()` / `cleanup()` / `close()`
- name：必填，str，应用唯一标识
- path：可选，str，可执行文件路径（未传且已注册模板时取模板值）
- args：可选，list[str]；cwd：可选，str；env：可选，dict（与系统环境合并）
- timeout：可选，float，terminate 等待秒数（默认 5.0）

## 4. 输出契约
- 成功：`launch()/terminate()/restart()` 返回 `True`；`is_running()` 返回 bool；`get_pid()` 返回 int 或 `None`；`list_launched()/list_templates()` 返回 dict 列表；`get_app_info()` 返回 dict 或 `None`
- 失败：`launch()` 未指定路径 / 文件不存在 / Popen 异常返回 `False`；`terminate()` 无记录返回 `False`；`restart()` 无模板且无记录返回 `False`
- 事件：无（进程状态由调用方轮询 list_launched）

## 5. 依赖声明
- 外部服务：被启动的外部应用（OBS、VTS、游戏、直播工具等，需预先安装）
- 内部模块：无（纯 subprocess 管理）
- 预先配置：启动前需 register_template 注册模板（或 launch 直接传 path）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 未指定路径 | launch 时 path 为空且无模板 | 返回 False，记录错误 |
| 文件不存在 | Popen 抛 FileNotFoundError | 返回 False，记录错误 |
| 启动异常 | Popen 其他异常 | 返回 False，记录错误 |
| 重复启动 | 同名已在运行 | 先 terminate 旧进程再启动 |
| 终止超时 | terminate 等待超时 | kill 强制结束并等待 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start | 否 | 无显式 start（构造即就绪） |
| close | 是 | terminate_all 终止全部已启动进程 |

## 8. 领域状态说明
- 状态项：`_templates`（应用模板表）、`_launched`（已启动进程：process/pid/path/args/started_at）、`_auto_start`（自启名单）
- 持久化：无（全部可重建）
- 恢复：close 时终止全部进程；重启后需重新 register_template
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