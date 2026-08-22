"""event_logger.py — 事件记录器（任务四：L0 落盘 + L1 循环缓冲）

订阅 EventBus 全量事件（* 通配符），完成分层记忆的最底层采集：
- L0：原始事件按天写入 data/memory/l0/YYYYMMDD.jsonl（批量 flush，不参与检索）
- L1：文本化后的循环缓冲（时间窗 + 容量），供检索/压缩消费
- 每条 L0 落盘后发布 MEMORY_EVENT_LOGGED（驱动压缩检查）

「说了/听了/做了/看了」全量采集：订阅现成事件即可（screen/live2d/game/speech 等），
各模块无需改日志（规格书 4.4）。

# 模块内容清单 — event_logger

## 1. 模块身份标识
- 所属调度官：memory（记忆调度官）
- 能力名：无独立能力（内部采集器，被 memory:recall/compress 消费）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| l0_dir | 否 | data/memory/l0 | Path | L0 原始日志目录 |
| config | 否 | MemoryConfig() | MemoryConfig | 保留期/时间窗/容量等 |
| batch_size | 否 | 20 | int 1..500 | L0 批量落盘条数 |

## 3. 输入契约
- 输入格式：`EventLogger(event_bus, config=None, l0_dir=None)`
- event_bus：EventBus 实例（必填）
- 输入格式：`start()` 订阅全量事件；`stop()` 取消订阅并 flush
- 输入格式：`l1_entries(after_seq=None)` / `l1_total_chars(after_seq=None)`

## 4. 输出契约
- 成功：L0 行 `{"event","ts","data"}` 落盘 JSONL；L1 条目
  `{"memory_id","event","role","content","timestamp","session_id"}`；发布 MEMORY_EVENT_LOGGED
- 失败：单条事件记录异常仅记日志，不影响总线
- 事件：发布 MEMORY_EVENT_LOGGED（event/ts）

## 5. 依赖声明
- 外部服务：无
- 内部模块：src.shared.events（事件常量）、src.shared.config_loader（PROJECT_ROOT）
- 预先配置：无（l0_dir 缺省自动创建）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 磁盘写入异常 | l0 目录不可写等 | 记录日志跳过该批，下次 flush 重试 |
| JSON 序列化失败 | data 含不可序列化对象 | default=str 兜底 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start() | 是 | 订阅全量事件 + 清理超期 L0 文件 |
| stop() | 是 | 取消订阅 + flush + 关闭文件句柄 |
| flush() | 是 | 批量落盘 L0 缓冲，返回落盘条数 |

## 8. 领域状态说明
- 状态项：_l1（deque 循环缓冲）、_pending（L0 待落盘缓冲）、_seq（全局单调）、_file（当日句柄）
- 持久化：L0 落盘 JSONL（按天，retention_days 清理）；L1 纯内存
- 恢复：L1 重启清空（短期记忆特性）；L0 从磁盘恢复
"""
import json
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from src.orchestrators.memory_orchestrator.memory_config import MemoryConfig
from src.shared import events as ev
from src.shared.config_loader import PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_L0_DIR = PROJECT_ROOT / "data" / "memory" / "l0"

# L1 缓冲排除的高频/纯状态类事件（L0 仍全量记录）：
# 10Hz 参数帧、光标位置推送、前端状态推送、音频分片就绪等无文本语义或高频刷屏。
# 从 events 常量构建，避免业务代码手写事件字符串（纪律 D5）。
_L1_EXCLUDE = frozenset({
    ev.LIVE2D_PARAMS_BATCH,
    ev.SCREEN_CURSOR_STATE,
    ev.FRONTEND_STATUS_UPDATE,
    ev.AUDIO_SEGMENT_READY,
    ev.MEMORY_EVENT_LOGGED,  # 防递归：自身事件不进缓冲也不重发
})

# 文本化优先取字段（事件 payload 常见文本字段）
_TEXT_FIELDS = ("content", "text", "message", "reply")


class EventLogger:
    """全量事件采集：L0 落盘 + L1 循环缓冲。"""

    def __init__(self, event_bus, config: Optional[MemoryConfig] = None,
                 l0_dir: Optional[str] = None, batch_size: int = 20):
        self._bus = event_bus
        self._config = config or MemoryConfig()
        self._l0_dir = Path(l0_dir) if l0_dir else DEFAULT_L0_DIR
        self._l0_dir.mkdir(parents=True, exist_ok=True)
        self._batch_size = max(1, int(batch_size))
        self._lock = threading.RLock()
        self._l1: Deque[Dict[str, Any]] = deque(maxlen=self._config.l1_max_entries)
        self._pending: List[str] = []
        self._seq = 0
        self._file = None
        self._file_date = ""
        self._started = False

    # ---------- 生命周期 ----------

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._cleanup_old_files()
            self._bus.subscribe("*", self._on_event, name="EventLogger")
            self._started = True
            logger.info("[EventLogger] 已启动（L0=%s, L1 窗口 %.0fs/上限 %d）",
                        self._l0_dir, self._config.l1_window_sec,
                        self._config.l1_max_entries)

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._bus.unsubscribe("*", self._on_event)
            self.flush()
            self._close_handle()
            self._started = False
            logger.info("[EventLogger] 已停止")

    def flush(self) -> int:
        """批量落盘 L0 缓冲，返回落盘条数。"""
        with self._lock:
            if not self._pending:
                return 0
            lines, self._pending = self._pending, []
        try:
            handle = self._ensure_handle()
            handle.write("".join(lines))
            handle.flush()
            return len(lines)
        except OSError as e:
            logger.error("[EventLogger] L0 落盘失败: %s", e)
            # 落盘失败：重新放回缓冲，下次重试（防止静默丢数据）
            with self._lock:
                self._pending = lines + self._pending
            return 0

    # ---------- 事件处理 ----------

    def _on_event(self, event: str, **data) -> None:
        """全量事件处理器（EventBus 同步调用，需快速返回）。"""
        if event == ev.MEMORY_EVENT_LOGGED:
            return  # 防递归：自身事件不重发
        ts = time.time()
        try:
            line = json.dumps({"event": event, "ts": round(ts, 3), "data": data},
                              ensure_ascii=False, default=str)
        except (TypeError, ValueError) as e:  # pragma: no cover - 兜底
            logger.warning("[EventLogger] L0 序列化失败 %s: %s", event, e)
            line = json.dumps({"event": event, "ts": round(ts, 3),
                               "data": {"_serialize_error": str(e)}}, ensure_ascii=False)
        with self._lock:
            self._seq += 1
            seq = self._seq
            if event not in _L1_EXCLUDE:
                text = self._to_text(event, data)
                if text:
                    self._l1.append({
                        "memory_id": f"e{seq}",
                        "event": event,
                        "role": "system",
                        "content": text[:self._config.l1_max_content_chars],
                        "timestamp": ts,
                        "session_id": data.get("session_id", "default"),
                    })
            self._pending.append(line)
            if len(self._pending) >= self._batch_size:
                lines, self._pending = self._pending, []
                self._write_lines(lines)
        # L0 落盘完成 → 通知（驱动压缩检查）；payload 键名避开 EventBus 保留字 event
        self._bus.publish(ev.MEMORY_EVENT_LOGGED, source_event=event, ts=ts)

    def _write_lines(self, lines: List[str]) -> None:
        try:
            handle = self._ensure_handle()
            handle.write("".join(lines))
            handle.flush()
        except OSError as e:
            logger.error("[EventLogger] L0 批量落盘失败: %s", e)
            with self._lock:
                self._pending = lines + self._pending

    # ---------- L1 读取（供检索/压缩） ----------

    def l1_entries(self, after_seq: Optional[int] = None,
                   window_sec: Optional[float] = None) -> List[Dict[str, Any]]:
        """L1 缓冲条目（新→旧；after_seq 增量；window_sec 时间窗过滤）。"""
        window = self._config.l1_window_sec if window_sec is None else window_sec
        cutoff = time.time() - window
        with self._lock:
            items = [e for e in self._l1 if e["timestamp"] >= cutoff]
        if after_seq is not None:
            items = [e for e in items if int(e["memory_id"][1:]) > after_seq]
        items.sort(key=lambda e: e["timestamp"], reverse=True)
        return items

    def l1_total_chars(self, after_seq: Optional[int] = None) -> int:
        """L1 当前累计文本量（压缩阈值检查用）。"""
        return sum(len(e["content"]) for e in self.l1_entries(after_seq=after_seq))

    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def count_l1(self) -> int:
        return len(self.l1_entries())

    # ---------- 内部实现 ----------

    @staticmethod
    def _pick_text(data: Dict[str, Any]) -> str:
        """从事件 payload 提取文本字段。"""
        for key in _TEXT_FIELDS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _to_text(self, event: str, data: Dict[str, Any]) -> str:
        """事件 → 自然语言文本（L1 存储形态）。"""
        text = self._pick_text(data)
        if event == ev.DANMAKU_RECEIVED:
            return f"观众弹幕：{text or data.get('raw', '')}"
        if event == ev.GIFT_RECEIVED:
            return f"收到礼物：{data.get('gift_name', '')}×{data.get('num', data.get('count', ''))}"
        if event == ev.SUPERCHAT_RECEIVED:
            return f"收到醒目留言：{text}"
        if event == ev.GUARD_RECEIVED:
            return f"上舰：{data.get('user', data.get('username', ''))}"
        if event == ev.AUDIENCE_ENTERED:
            return f"观众进场：{data.get('user', data.get('username', ''))}"
        if event == ev.LLM_RESPONDED:
            return f"我说：{text}" if text else f"LLM 响应：{data.get('capability', '')}"
        if event == ev.SPEECH_COMPLETED:
            return f"发言完成：{text}"
        if event == ev.TTS_COMPLETED:
            return f"语音合成：{text}"
        if event == ev.EMOTION_EXTRACTED:
            return f"情绪识别：{data.get('emotion', '')}"
        if event == ev.SCREEN_CURSOR_ACTION:
            label = data.get("label", "")
            action = data.get("action", "")
            return f"光标操作：{action} {label}".strip()
        if event == ev.GAME_OP_OPERATION:
            return f"游戏操作：{data.get('action', '')} {data.get('params', '')}".strip()
        if event == ev.GAME_OP_FEEDBACK:
            return f"游戏反馈：{data.get('action', '')} ok={data.get('ok', '')}"
        if event == ev.MEMORY_STORED:
            return f"记忆写入：{text}"
        if event == ev.INPUT_CLASSIFIED:
            return f"输入分类：{data.get('type', '')} 优先级={data.get('priority', '')}"
        if event == ev.SPEECH_ARBITRATED:
            return f"发言仲裁：role={data.get('role', '')} hit={data.get('rule_hit', '')}"
        if text:
            return text
        return event

    def _ensure_handle(self):
        """按天切换 L0 文件句柄。"""
        today = time.strftime("%Y%m%d")
        if self._file is None or self._file_date != today:
            self._close_handle()
            path = self._l0_dir / f"{today}.jsonl"
            self._file = open(path, "a", encoding="utf-8")
            self._file_date = today
        return self._file

    def _close_handle(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:  # pragma: no cover
                pass
            self._file = None
            self._file_date = ""

    def _cleanup_old_files(self) -> None:
        """删除超过保留期的 L0 日志文件（按文件名日期）。"""
        cutoff = time.time() - self._config.l0_retention_days * 86400
        try:
            for path in self._l0_dir.glob("*.jsonl"):
                name = path.stem
                if len(name) != 8 or not name.isdigit():
                    continue
                import datetime
                try:
                    file_date = datetime.datetime.strptime(name, "%Y%m%d").timestamp()
                except ValueError:
                    continue
                if file_date < cutoff:
                    path.unlink(missing_ok=True)
                    logger.info("[EventLogger] 清理超期 L0 文件: %s", path.name)
        except OSError as e:  # pragma: no cover
            logger.warning("[EventLogger] L0 清理失败: %s", e)
