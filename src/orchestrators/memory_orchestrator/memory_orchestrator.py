"""memory_orchestrator.py — 记忆调度官主类（规格书 5.4）

能力：memory:store / retrieve / consolidate / get_history。
职责边界（5.5）：不主动固化，由指挥官按会话节奏触发 memory:consolidate。

# 模块内容清单（8 项契约）
1. 模块身份标识：memory 调度官 · memory_orchestrator · 能力 memory:store / retrieve / consolidate / get_history
2. 配置契约：db_path（可选，长期记忆 SQLite 路径）
3. 输入契约：handle(command) 接收 {"capability": "memory:*", "payload": {"content","query","session_id","character_id","k","mode","limit"}}；character_id 可选，用于按角色分桶隔离记忆（与 session_id 组合为 bucket，缺省沿用旧版全局桶行为）
4. 输出契约：返回 {"ok": bool, "data": {...}, "error": str|null}；发布 MEMORY_STORED / MEMORY_RETRIEVED / MEMORY_CONSOLIDATED
5. 依赖声明：registry、retriever、long_term、short_term、src.shared.events
6. 错误定义：content 为空返回 {"ok": false, "error": "content 必填"}
7. 生命周期方法：start()、stop()（关闭长期连接）、health()、handle() 能力分发
8. 领域状态说明：_short（ShortTermMemory）、_long（LongTermMemory）、_started
"""
import logging
from typing import Any, Dict, List, Optional

from src.orchestrators.memory_orchestrator import registry
from src.orchestrators.memory_orchestrator import retriever
from src.orchestrators.memory_orchestrator.long_term import LongTermMemory
from src.orchestrators.memory_orchestrator.short_term import ShortTermMemory
from src.shared.events import MEMORY_CONSOLIDATED, MEMORY_RETRIEVED, MEMORY_STORED

logger = logging.getLogger(__name__)


class MemoryOrchestrator:
    """记忆调度官。"""

    name = "memory"

    def __init__(self, event_bus, db_path: Optional[str] = None):
        self._event_bus = event_bus
        self._short = ShortTermMemory()
        self._long = LongTermMemory(db_path=db_path)
        self._started = False
        registry.bind(self.handle)

    def capabilities(self) -> List[str]:
        return registry.capabilities()

    def start(self) -> None:
        self._started = True
        logger.info("[MemoryOrchestrator] 已启动")

    async def handle(self, command: Dict[str, Any]) -> Dict[str, Any]:
        capability = command.get("capability", "")
        payload = command.get("payload", {})
        if capability == "memory:store":
            return self._store(payload)
        if capability == "memory:retrieve":
            return self._retrieve(payload)
        if capability == "memory:consolidate":
            return self._consolidate(payload)
        if capability == "memory:get_history":
            return self._get_history(payload)
        return {"ok": False, "data": {}, "error": f"unknown capability: {capability}"}

    def health(self) -> Dict[str, Any]:
        return {"status": "ok" if self._started else "down",
                "detail": f"long_term={self._long.count()}"}

    def stop(self) -> None:
        self._started = False
        self._long.close()

    # ---------- 内部实现 ----------

    @staticmethod
    def _bucket(session_id: str, character_id: str = "") -> str:
        """记忆分桶：character_id 存在时按角色隔离。"""
        return f"{session_id}:{character_id}" if character_id else session_id

    def _store(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        content = payload.get("content", "")
        if not content:
            return {"ok": False, "data": {}, "error": "content 必填"}
        session_id = payload.get("session_id", "default")
        bucket = self._bucket(session_id, payload.get("character_id", ""))
        memory_id = self._short.append(bucket, payload.get("role", "user"), content)
        self._event_bus.publish(MEMORY_STORED, memory_id=memory_id, session_id=session_id)
        return {"ok": True, "data": {"memory_id": memory_id}, "error": None}

    def _retrieve(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        query = payload.get("query", "")
        k = int(payload.get("k", 5))
        session_id = payload.get("session_id", "")
        bucket = self._bucket(session_id, payload.get("character_id", ""))
        mode = payload.get("mode", "hybrid")  # keyword / vector / hybrid（默认）
        short_entries = self._short.get_history(bucket) if bucket else self._short.all_entries()
        long_entries = self._long.retrieve(query, k) if query else []
        if query:
            if mode == "vector":
                matched = retriever.vector_retrieve(
                    long_entries or short_entries, query, k)
            elif mode == "keyword":
                matched = retriever.keyword_retrieve(
                    long_entries or short_entries, query, k)
            else:
                matched = retriever.hybrid_retrieve(
                    long_entries or short_entries, query, k)
            merged = retriever.merge_results(
                [e for e in short_entries if e not in matched], matched, k)
        else:
            merged = retriever.merge_results(short_entries, long_entries, k)
        self._event_bus.publish(MEMORY_RETRIEVED, query=query, count=len(merged))
        return {"ok": True, "data": {"memories": merged}, "error": None}

    def _consolidate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """短期 → 长期固化（规格书 5.5：由指挥官触发）。"""
        session_id = payload.get("session_id", "")
        bucket = self._bucket(session_id, payload.get("character_id", ""))
        entries = self._short.get_history(bucket) if bucket else self._short.all_entries()
        count = 0
        for entry in entries:
            self._long.store(content=entry["content"], role=entry["role"],
                             session_id=entry["session_id"])
            count += 1
        self._short.clear(bucket or None)  # 清空与读取同键：bucket 存在清桶，否则清全部
        self._event_bus.publish(MEMORY_CONSOLIDATED, count=count)
        return {"ok": True, "data": {"consolidated": count}, "error": None}

    def _get_history(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        session_id = payload.get("session_id", "default")
        bucket = self._bucket(session_id, payload.get("character_id", ""))
        limit = int(payload.get("limit", 20))
        history = self._short.get_history(bucket, limit)
        return {"ok": True, "data": {"history": history}, "error": None}
