"""long_term.py — 长期记忆（SQLite 持久化）

规格书 603 行：持久化用本地 JSON/SQLite（旧 permanent_memory_store.py 模式），后续可升级向量库。

# 模块内容清单（8 项契约）
1. 模块身份标识：memory 调度官 · long_term · 能力 memory:store/retrieve/consolidate 的长期存储实现
2. 配置契约：db_path（默认 data/memory/long_term.db）
3. 输入契约：store(content, role, tags, session_id) / retrieve(query, k) / get_history(session_id, limit) / count() / close()
4. 输出契约：store 返回 memory_id；retrieve 返回按关键词打分排序的条目 list；get_history 返回会话历史
5. 依赖声明：sqlite3、json、pathlib.Path、src.shared.config_loader（PROJECT_ROOT）
6. 错误定义：SQLite 操作异常向上抛出（由调用方处理）
7. 生命周期方法：close() 关闭连接；构造时建表
8. 领域状态说明：_conn（SQLite 连接）、_lock、_path；数据持久化于磁盘
"""
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.shared.config_loader import PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_DB = PROJECT_ROOT / "data" / "memory" / "long_term.db"


class LongTermMemory:
    """SQLite 长期记忆。"""

    def __init__(self, db_path: Optional[str] = None):
        self._path = Path(db_path) if db_path else DEFAULT_DB
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._lock = threading.RLock()
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                role TEXT DEFAULT '',
                tags TEXT DEFAULT '[]',
                session_id TEXT DEFAULT '',
                created_at REAL
            )
        """)
        self._conn.commit()

    def store(self, content: str, role: str = "", tags: Optional[List[str]] = None,
              session_id: str = "") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO memories (content, role, tags, session_id, created_at) VALUES (?,?,?,?,?)",
                (content, role, json.dumps(tags or [], ensure_ascii=False), session_id, time.time()),
            )
            self._conn.commit()
            return cur.lastrowid

    def retrieve(self, query: str, k: int = 5) -> List[Dict[str, Any]]:
        """关键词模糊检索。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, content, role, tags, session_id, created_at FROM memories"
            ).fetchall()
        terms = [t for t in query.lower().split() if t]
        scored = []
        for row in rows:
            score = 0
            text = f"{row[1]} {row[3]}".lower()
            for t in terms:
                if t in text:
                    score += 1
            if score > 0:
                scored.append((score, row))
        scored.sort(key=lambda x: -x[0])
        return [self._row_to_dict(r) for _, r in scored[:k]]

    def get_history(self, session_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, content, role, tags, session_id, created_at FROM memories "
                "WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()
            return row[0]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        return {"memory_id": f"l{row[0]}", "content": row[1], "role": row[2],
                "tags": json.loads(row[3] or "[]"), "session_id": row[4],
                "timestamp": row[5]}
