"""mc_bridge.py — MC 实况桥（P3）

管理 Node.js Bot 子进程（旧项目 deploy/mc_bot/bot.js 的行为引擎参考），
通过 stdin/stdout 管道以 JSON-Line 协议通信。bot.js 未迁移时返回清晰错误提示。

# 模块内容清单（8 项契约）
1. 模块身份标识：game 调度官 · mc_bridge · 能力 game:mc_start / game:mc_stop
2. 配置契约：bot_path(默认 deploy/mc_bot/bot.js) / node_args / protocol="jsonl"
3. 输入契约：start(mode)、stop()、send(command: dict)、read_line(timeout)
4. 输出契约：start/stop 返回 Dict；send 返回 bool；read_line 返回 dict|None
5. 依赖声明：node 可执行文件（absent 时 bot 无法启动，返回 BOT_NOT_FOUND）
6. 错误定义：bot.js 缺失 → {"started": False, "error_code": "BOT_NOT_FOUND"}；
             进程退出/超时 → send 返回 False，read_line 返回 None
7. 生命周期方法：start()/stop()/running 属性；reader 线程随 start 启动、stop 结束
8. 领域状态说明：_proc Popen + _reader 线程；重启需重新 start
"""
import json
import logging
import queue
import subprocess
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_BOT = "deploy/mc_bot/bot.js"


class MCBridge:
    """MC 实况桥：启动/停止 Node.js Bot 子进程，并基于 JSON-Line 通信。"""

    def __init__(self, bot_path: Optional[str] = None, node_args: Optional[list] = None):
        self._bot_path = bot_path or _DEFAULT_BOT
        self._node_args = node_args or []
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._q: "queue.Queue[Optional[Dict]]" = queue.Queue()
        self._stop_read = threading.Event()
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, mode: str = "live") -> Dict[str, Any]:
        """启动 Bot 进程并开启 stdout 读取线程（JSON-Line）。"""
        if self.running:
            return {"started": True, "detail": "already running"}
        import os
        if not os.path.exists(self._bot_path):
            logger.warning("[MCBridge] bot.js 未迁移: %s", self._bot_path)
            return {"started": False, "detail": f"bot.js not found: {self._bot_path}",
                    "error_code": "BOT_NOT_FOUND"}
        try:
            self._proc = subprocess.Popen(
                ["node", self._bot_path, "--mode", mode] + self._node_args,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
        except FileNotFoundError:
            logger.error("[MCBridge] node 可执行文件不存在，无法启动 bot")
            return {"started": False, "detail": "node not found in PATH",
                    "error_code": "NODE_NOT_FOUND"}
        except Exception as e:
            logger.error("[MCBridge] 启动失败: %s", e)
            return {"started": False, "detail": str(e), "error_code": "START_FAILED"}
        self._stop_read = threading.Event()
        self._q = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="mc-bridge-reader")
        self._reader.start()
        logger.info("[MCBridge] Bot 已启动 (mode=%s, pid=%s)", mode, self._proc.pid)
        return {"started": True, "pid": self._proc.pid, "mode": mode}

    def stop(self) -> Dict[str, Any]:
        """停止 Bot 进程并结束读取线程。"""
        if self.running:
            self._stop_read.set()
            if self._proc and self._proc.stdin:
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            logger.info("[MCBridge] Bot 已停止")
        self._proc = None
        if self._reader is not None and self._reader.is_alive():
            self._reader.join(timeout=3)
        self._reader = None
        return {"stopped": True, "detail": "not running" if not self.running else "stopped"}

    # ---------- 通信 ----------

    def send(self, command: Dict[str, Any]) -> bool:
        """向 bot 发送一条 JSON-Line 命令。返回是否成功写入管道。"""
        if not self.running or self._proc is None or self._proc.stdin is None:
            logger.warning("[MCBridge] bot 未运行，无法发送命令")
            return False
        try:
            line = json.dumps(command, ensure_ascii=False) + "\n"
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
            return True
        except Exception as e:
            logger.error("[MCBridge] 发送失败: %s", e)
            return False

    def read_line(self, timeout: float = 0.5) -> Optional[Dict[str, Any]]:
        """读取一条 bot 消息（JSON-Line）。超时返回 None。"""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def _read_loop(self):
        while not self._stop_read.is_set():
            if self._proc is None or self._proc.stdout is None:
                break
            line = self._proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                self._q.put(json.loads(line))
            except json.JSONDecodeError:
                logger.debug("[MCBridge] 忽略非 JSON 输出: %s", line[:80])
        # 进程退出 → 推送哨兵
        self._q.put(None)

    def get_stats(self) -> Dict[str, Any]:
        return {"running": self.running,
                "pid": self._proc.pid if self._proc else None,
                "bot_path": self._bot_path,
                "queued_messages": self._q.qsize()}