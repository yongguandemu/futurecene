"""logger.py — 统一日志（规格书 6.3）

- 按模块名获取日志器：logger.get("tts") 只输出 tts 域日志。
- 日志级别可运行时调整：set_level(module, level) / set_global_level(level)。
- 结构化输出：时间 | 级别 | 模块 | 消息（事件名由调用方放入消息）。

用法：
    from src.shared import logger
    log = logger.get("tts")
    log.info("合成完成 audio_id=%s", audio_id)

# 模块内容清单（8 项契约）
1. 模块身份标识：shared · logger · 对外接口 get()/set_level()/set_global_level()
2. 配置契约：无外部配置（统一格式/日期格式常量，默认 INFO 级别）
3. 输入契约：get(module) 模块名；set_level(module, level) 模块+级别
4. 输出契约：返回挂载统一 Handler 的 logging.Logger；结构化输出 时间|级别|模块|消息
5. 依赖声明：logging、sys、typing
6. 错误定义：无显式异常（非法级别由 logging 层处理）
7. 生命周期方法：get()（初始化并挂载 Handler）/set_level()/set_global_level()
8. 领域状态说明：_LOG_FORMAT/_LOG_DATEFMT 格式常量；已注册日志器由 logging.root.manager 管理
"""
import logging
import sys
from typing import Union

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
Level = Union[str, int]


def _build_handler() -> logging.Handler:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
    return handler


def get(module: str) -> logging.Logger:
    """按模块名获取日志器；首次获取时挂载统一 Handler。"""
    logger = logging.getLogger(module)
    if not logger.handlers:
        logger.addHandler(_build_handler())
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def set_level(module: str, level: Level) -> None:
    """调整单个模块日志级别（如 logger.set_level("tts", "DEBUG")）。"""
    get(module).setLevel(level)


def set_global_level(level: Level) -> None:
    """调整全部已注册日志器级别。"""
    for name in logging.root.manager.loggerDict:
        logging.getLogger(name).setLevel(level)
