"""short_term.py — 短期记忆（对话历史内存缓存，按会话 ID）

# 模块内容清单（8 项契约）
1. 模块身份标识：memory 调度官 · short_term · 能力 memory:store/get_history/consolidate 的短期存储实现
2. 配置契约：max_entries_per_session(50)
3. 输入契约：append(session_id, role, content) / get_history(session_id, limit) / all_entries() / clear(session_id)
4. 输出契约：append 返回 memory_id（s{seq}）；get_history 返回会话历史（新→旧）
5. 依赖声明：threading、collections.deque、time
6. 错误定义：无（纯内存数据结构，不抛业务异常）
7. 生命周期方法：无（纯数据结构）
8. 领域状态说明：_sessions（session_id → deque）、_max、_seq、_lock；纯内存，重启清空
"""
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional


class ShortTermMemory:
    """按 session_id 维护的对话历史内存缓存。"""

    def __init__(self, max_entries_per_session: int = 50):
        self._sessions: Dict[str, deque] = {}
        self._max = max_entries_per_session
        self._lock = threading.RLock()
        self._seq = 0

    def append(self, session_id: str, role: str, content: str) -> str:
        """追加一条记忆，返回 memory_id（形如 s{seq}）。"""
        with self._lock:
            self._seq += 1
            memory_id = f"s{self._seq}"
            entry = {"memory_id": memory_id, "role": role, "content": content,
                     "timestamp": time.time(), "session_id": session_id}
            q = self._sessions.setdefault(session_id, deque(maxlen=self._max))
            q.append(entry)
            return memory_id

    def get_history(self, session_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """返回会话历史（新→旧排序）。"""
        with self._lock:
            q = self._sessions.get(session_id)
            if not q:
                return []
            items = list(q)
        items.reverse()
        return items[-limit:] if limit else items

    def all_entries(self) -> List[Dict[str, Any]]:
        """全部短期记忆（供 consolidate 固化）。"""
        with self._lock:
            return [entry for q in self._sessions.values() for entry in q]

    def clear(self, session_id: Optional[str] = None) -> None:
        with self._lock:
            if session_id:
                self._sessions.pop(session_id, None)
            else:
                self._sessions.clear()
