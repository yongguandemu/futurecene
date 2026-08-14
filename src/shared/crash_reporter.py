"""crash_reporter.py — 崩溃捕获（P5）

注册全局 excepthook，未捕获异常写入 data/logs/crash_{ts}.log（含 traceback）。

# 模块内容清单（8 项契约）
1. 模块身份标识：shared · CrashReporter · 对外接口 install()/uninstall()
2. 配置契约：无外部配置（crash_dir 构造注入，默认 data/logs）
3. 输入契约：install() 无参；内部 _handle(exc_type, exc_value, exc_tb) 接收异常三元组
4. 输出契约：未捕获异常写入 data/logs/crash_{ts}.log（含 traceback），无返回值
5. 依赖声明：logging、sys、threading、time、traceback、datetime、pathlib、src.shared.config_loader
6. 错误定义：捕获所有未处理异常；崩溃日志写入失败仅记 error 不中断；保留原始 excepthook
7. 生命周期方法：install()/uninstall()
8. 领域状态说明：_dir 崩溃日志目录、_orig_hook 原始异常钩子引用
"""
import logging
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.shared.config_loader import PROJECT_ROOT

logger = logging.getLogger(__name__)

CRASH_DIR = PROJECT_ROOT / "data" / "logs"


class CrashReporter:
    """崩溃捕获与落盘。"""

    def __init__(self, crash_dir: str = ""):
        self._dir = Path(crash_dir) if crash_dir else CRASH_DIR
        self._orig_hook: Optional[object] = None

    def install(self) -> None:
        """安装全局异常钩子（进程级）。"""
        self._orig_hook = sys.excepthook
        sys.excepthook = self._handle
        # 线程异常也捕获
        threading.excepthook = lambda args: self._handle(args.exc_type, args.exc_value,
                                                         args.exc_traceback)
        logger.info("[CrashReporter] 崩溃钩子已安装")

    def uninstall(self) -> None:
        if self._orig_hook is not None:
            sys.excepthook = self._orig_hook  # type: ignore
            self._orig_hook = None

    def _handle(self, exc_type, exc_value, exc_tb) -> None:
        """写入崩溃日志。"""
        self._dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self._dir / f"crash_{ts}.log"
        content = (f"=== CRASH {datetime.now().isoformat()} ===\n"
                   f"Type: {exc_type.__name__}\n"
                   f"Value: {exc_value}\n"
                   f"Traceback:\n{''.join(traceback.format_tb(exc_tb))}")
        try:
            path.write_text(content, encoding="utf-8")
            logger.error("[CrashReporter] 崩溃已记录: %s", path)
        except OSError as e:
            logger.error("[CrashReporter] 崩溃日志写入失败: %s", e)
        # 保留原始钩子行为
        if self._orig_hook:
            self._orig_hook(exc_type, exc_value, exc_tb)
