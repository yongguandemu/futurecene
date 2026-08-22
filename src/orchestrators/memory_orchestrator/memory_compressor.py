"""memory_compressor.py — 中期记忆（L2）+ 压缩器（L1→L2→L3，任务四）

- MidTermMemory：SQLite 表 memory_l2（每段 500-1000 字摘要），保留数周-数月
- MemoryCompressor：累计文本量达阈值后异步压缩；L2→L3 归档（每段 100-300 字）
- 压缩模型统一 deepseek-v4-flash（禁用 glm-4.7-flash）；模型不可用 → 保留原文分段（降级）

# 模块内容清单 — memory_compressor

## 1. 模块身份标识
- 所属调度官：memory（记忆调度官）
- 能力名：无独立能力（memory:compress 的实现；被 memory:event_logged 事件驱动）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| mid_db_path | 否 | data/memory/l2.db | str | L2 中期记忆 SQLite 路径 |
| config | 否 | MemoryConfig() | MemoryConfig | 压缩阈值/摘要字数等 |
| summarize_fn | 否 | 无 | Callable[[str,int],str] | LLM 摘要函数（注入；缺省走降级） |
| switch_check | 否 | 恒 True | Callable[[str],bool] | 开关查询（memory_compression） |

## 3. 输入契约
- 输入格式：`MemoryCompressor(event_bus, mid_term, long_term, config, summarize_fn, l1_entries_fn, switch_check)`
- 输入格式：`compress_now(bucket="default", entries=None) -> bool`（同步压缩，测试/手动用）
- 输入格式：`consolidate_to_l3(bucket="default") -> int`（L2 摘要 → L3 归档条数）
- 输入格式：`start()` 订阅 MEMORY_EVENT_LOGGED；`stop()` 取消订阅

## 4. 输出契约
- 成功：压缩触发返回 True；摘要写入 L2（memory_id 形如 m2{id}）；归档返回条数
- 失败：累计量不足返回 False；模型不可用 → 原文分段落 L2（不抛异常）
- 事件：无（只消费 MEMORY_EVENT_LOGGED，不发布新事件）

## 5. 依赖声明
- 外部服务：LLM（经 summarize_fn 注入，compressor 不感知具体服务）
- 内部模块：memory_config、event_logger（经 l1_entries_fn 注入）、long_term、MidTermMemory
- 预先配置：无（SQLite 路径缺省自动建目录）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 摘要失败 | summarize_fn 抛异常/返回空 | 降级：原文分段落 L2 |
| 累计量不足 | 文本 < compress_min_chars | 不压缩，返回 False |
| SQLite 异常 | 磁盘问题 | 向上抛出（由 orchestrator 处理） |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start() | 是 | 订阅 MEMORY_EVENT_LOGGED，事件驱动阈值检查 |
| stop() | 是 | 取消订阅 |
| compress_now() | 是 | 同步执行 L1→L2 压缩（供测试与手动调用） |
| consolidate_to_l3() | 是 | L2→L3 归档 |

## 8. 领域状态说明
- 状态项：_last_seq（L1 增量游标）、_mid（MidTermMemory）、_long（LongTermMemory 引用）
- 持久化：L2 落 SQLite（memory_l2 表，archived 标记）；L3 复用 long_term
- 恢复：重启后 _last_seq 归零（L1 已清空，无重复压缩风险）
"""
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.orchestrators.memory_orchestrator.memory_config import MemoryConfig
from src.shared.config_loader import PROJECT_ROOT
from src.shared.events import MEMORY_EVENT_LOGGED

logger = logging.getLogger(__name__)

DEFAULT_L2_DB = PROJECT_ROOT / "data" / "memory" / "l2.db"

# L1 增量文本喂给 LLM 的上限（超长截断，控制成本）
_MAX_SUMMARIZE_INPUT_CHARS = 8000


class MidTermMemory:
    """中期记忆（L2）：SQLite 摘要存储。"""

    def __init__(self, db_path: Optional[str] = None):
        self._path = Path(db_path) if db_path else DEFAULT_L2_DB
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._lock = threading.RLock()
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_l2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bucket TEXT NOT NULL,
                summary TEXT NOT NULL,
                source_ids TEXT DEFAULT '[]',
                created_at REAL,
                archived INTEGER DEFAULT 0
            )
        """)
        self._conn.commit()

    def store(self, bucket: str, summary: str,
              source_ids: Optional[List[str]] = None) -> str:
        """写入一段 L2 摘要，返回 memory_id（形如 m2{id}）。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO memory_l2 (bucket, summary, source_ids, created_at) VALUES (?,?,?,?)",
                (bucket, summary, json.dumps(source_ids or [], ensure_ascii=False),
                 time.time()),
            )
            self._conn.commit()
            return f"m2{cur.lastrowid}"

    def get_recent(self, bucket: str, limit: int = 100,
                   include_archived: bool = False) -> List[Dict[str, Any]]:
        """最近摘要（新→旧）。archived 默认排除（已归档 L3 的不再重复处理）。"""
        with self._lock:
            sql = ("SELECT id, bucket, summary, source_ids, created_at, archived "
                   "FROM memory_l2 WHERE bucket=?")
            args: List[Any] = [bucket]
            if not include_archived:
                sql += " AND archived=0"
            sql += " ORDER BY created_at DESC LIMIT ?"
            args.append(limit)
            rows = self._conn.execute(sql, tuple(args)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count(self, bucket: Optional[str] = None) -> int:
        with self._lock:
            if bucket:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM memory_l2 WHERE bucket=?", (bucket,)).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) FROM memory_l2").fetchone()
            return row[0]

    def mark_archived(self, memory_id: str) -> bool:
        """标记 L2 已归档（升 L3 后防止重复）。memory_id 形如 m2{id}。"""
        if not memory_id.startswith("m2"):
            return False
        try:
            row_id = int(memory_id[2:])
        except ValueError:
            return False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE memory_l2 SET archived=1 WHERE id=?", (row_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        return {"memory_id": f"m2{row[0]}", "bucket": row[1], "summary": row[2],
                "source_ids": json.loads(row[3] or "[]"), "timestamp": row[4],
                "archived": bool(row[5])}


class MemoryCompressor:
    """分层压缩器：L1 → L2（摘要）→ L3（归档）。"""

    def __init__(self, event_bus, mid_term: MidTermMemory, long_term,
                 config: Optional[MemoryConfig] = None,
                 summarize_fn: Optional[Callable[[str, int], str]] = None,
                 l1_entries_fn: Optional[Callable[[Optional[int]], List[Dict[str, Any]]]] = None,
                 switch_check: Optional[Callable[[str], bool]] = None):
        self._bus = event_bus
        self._mid = mid_term
        self._long = long_term
        self._config = config or MemoryConfig()
        self._summarize_fn = summarize_fn
        self._l1_entries_fn = l1_entries_fn
        self._switch_check = switch_check or (lambda name: True)
        self._last_seq = 0
        self._lock = threading.RLock()
        self._threads: List[threading.Thread] = []
        self._started = False

    # ---------- 生命周期 ----------

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._bus.subscribe(MEMORY_EVENT_LOGGED, self._on_event_logged,
                                name="MemoryCompressor")
            self._started = True
            logger.info("[MemoryCompressor] 已启动（阈值 %d-%d 字）",
                        self._config.compress_min_chars, self._config.compress_max_chars)

    def set_summarize_fn(self, summarize_fn: Optional[Callable[[str, int], str]]) -> None:
        """装配层延迟注入摘要函数（LLM 调度官就绪后调用）。"""
        self._summarize_fn = summarize_fn

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._bus.unsubscribe(MEMORY_EVENT_LOGGED, self._on_event_logged)
            self._started = False
            logger.info("[MemoryCompressor] 已停止")

    # ---------- 事件驱动 ----------

    def _on_event_logged(self, event: str, **data) -> None:
        """L0 落盘事件 → 检查累计量，达阈值异步压缩（不阻塞总线）。"""
        if not self._switch_check("memory_compression"):
            return
        if self._l1_entries_fn is None:
            return
        try:
            total = self.l1_total_chars()
        except Exception:  # pragma: no cover - 防御
            return
        if total >= self._config.compress_min_chars:
            self._spawn_compress()

    def _spawn_compress(self) -> threading.Thread:
        """异步执行压缩（daemon 线程）。"""
        thread = threading.Thread(target=self.compress_now, args=("default",),
                                  daemon=True, name="MemoryCompress")
        with self._lock:
            self._threads.append(thread)
        thread.start()
        return thread

    # ---------- 核心操作 ----------

    def l1_total_chars(self, after_seq: Optional[int] = None) -> int:
        """L1 增量累计文本量（阈值检查）。"""
        entries = self._l1_entries_fn(after_seq=after_seq)
        return sum(len(e.get("content", "")) for e in entries)

    def compress_now(self, bucket: str = "default",
                     entries: Optional[List[Dict[str, Any]]] = None) -> bool:
        """同步执行 L1→L2 压缩。

        返回 True 表示本次触发并完成压缩；累计量不足返回 False。
        模型不可用（summarize_fn 缺省/失败）→ 原文分段落 L2（规格书降级路径）。
        """
        if entries is None:
            if self._l1_entries_fn is None:
                return False
            entries = self._l1_entries_fn(after_seq=self._last_seq)
        texts = [e.get("content", "") for e in entries if e.get("content")]
        if sum(len(t) for t in texts) < self._config.compress_min_chars:
            return False
        joined = "\n".join(texts)[:_MAX_SUMMARIZE_INPUT_CHARS]
        source_ids = [e.get("memory_id", "") for e in entries]
        summary = self._summarize(joined, self._config.l2_summary_max)
        if summary:
            self._mid.store(bucket, summary, source_ids)
            logger.info("[MemoryCompressor] L1→L2 摘要落库（%d 条 → %d 字）",
                        len(source_ids), len(summary))
        else:
            for segment in self._fallback_segments(joined):
                self._mid.store(bucket, segment, source_ids)
            logger.info("[MemoryCompressor] 模型不可用，降级原文分段落 L2（%d 段）",
                        self._mid.count(bucket))
        self._advance_cursor(entries)
        return True

    def consolidate_to_l3(self, bucket: str = "default") -> int:
        """L2 摘要 → L3 归档（每段 ≤ l3_entry_max 字）。返回归档条数。"""
        entries = self._mid.get_recent(bucket)
        count = 0
        for entry in entries:
            short = self._summarize(entry["summary"], self._config.l3_entry_max)
            if not short:
                short = entry["summary"][:self._config.l3_entry_max]
            if not short.strip():
                continue
            self._long.store(content=short.strip(), role="system",
                             tags=["l2-summary"], session_id=bucket)
            self._mid.mark_archived(entry["memory_id"])
            count += 1
        if count:
            logger.info("[MemoryCompressor] L2→L3 归档 %d 条（bucket=%s）", count, bucket)
        return count

    # ---------- 内部实现 ----------

    def _summarize(self, text: str, max_chars: int) -> str:
        """调 LLM 摘要（注入函数）；缺省/异常/空返回 → 空串（触发降级）。"""
        if self._summarize_fn is None:
            return ""
        try:
            result = self._summarize_fn(text, max_chars)
        except Exception as e:  # pragma: no cover - 防御
            logger.warning("[MemoryCompressor] 摘要调用异常，降级原文分段: %s", e)
            return ""
        if not result or not isinstance(result, str):
            return ""
        result = result.strip()
        return result[:max_chars * 3] if result else ""  # 兜底截断，防异常超长

    @staticmethod
    def _fallback_segments(text: str, max_chars: Optional[int] = None) -> List[str]:
        """降级：原文按 max_chars 分段（规格书：模型不可用保留原文分段）。"""
        limit = max_chars or 800
        segments = []
        remaining = text
        while len(remaining) > limit:
            segments.append(remaining[:limit])
            remaining = remaining[limit:]
        if remaining.strip():
            segments.append(remaining)
        return [s for s in segments if s.strip()]

    def _advance_cursor(self, entries: List[Dict[str, Any]]) -> None:
        """增量游标推进到本次处理的最大 L1 序号。"""
        seqs = []
        for e in entries:
            mid = e.get("memory_id", "")
            if mid.startswith("e") and mid[1:].isdigit():
                seqs.append(int(mid[1:]))
        if seqs:
            with self._lock:
                self._last_seq = max(seqs)
