"""memory_orchestrator.py — 记忆调度官主类（规格书 5.4 + 任务四分层记忆）

能力：memory:store / retrieve / consolidate / get_history（既有，行为不变）
     + memory:recall（分层检索 L1/L2/L3）/ compress（手动压缩）/ review（世界书提案）
职责边界（5.5）：不主动固化，由指挥官按会话节奏触发 memory:consolidate；
压缩由 EventLogger 事件驱动（memory:event_logged → MemoryCompressor 阈值检查）。

# 模块内容清单（8 项契约）
1. 模块身份标识：memory 调度官 · memory_orchestrator · 能力 memory:store/retrieve/consolidate/get_history/recall/compress/review
2. 配置契约：db_path（长期记忆 SQLite 路径）；config_loader（memory.* 段）；summarize_fn（LLM 摘要注入）；switch_check（开关查询）
3. 输入契约：handle(command) 接收 {"capability": "memory:*", "payload": {...}}；character_id 可选，用于按角色分桶隔离记忆（与 session_id 组合为 bucket，缺省沿用旧版全局桶行为）
4. 输出契约：返回 {"ok": bool, "data": {...}, "error": str|null}；发布 MEMORY_STORED / MEMORY_RETRIEVED / MEMORY_CONSOLIDATED
5. 依赖声明：registry、retriever、long_term、short_term、memory_config、event_logger、memory_compressor、review、src.shared.events
6. 错误定义：content 为空返回 {"ok": false, "error": "content 必填"}；memory_compression 关闭时压缩被拒
7. 生命周期方法：start()、stop()（停止订阅 + 关闭长期/中期连接）、health()、handle() 能力分发
8. 领域状态说明：_short（ShortTermMemory）、_long（LongTermMemory）、_mid（MidTermMemory）、_logger（EventLogger）、_compressor（MemoryCompressor）、_reviewer（MemoryReview）、_started
"""
import logging
from typing import Any, Callable, Dict, List, Optional

from src.orchestrators.memory_orchestrator import registry
from src.orchestrators.memory_orchestrator import retriever
from src.orchestrators.memory_orchestrator.event_logger import EventLogger
from src.orchestrators.memory_orchestrator.long_term import LongTermMemory
from src.orchestrators.memory_orchestrator.memory_compressor import (
    MemoryCompressor,
    MidTermMemory,
)
from src.orchestrators.memory_orchestrator.memory_config import MemoryConfig
from src.orchestrators.memory_orchestrator.review import MemoryReview
from src.orchestrators.memory_orchestrator.short_term import ShortTermMemory
from src.shared.events import MEMORY_CONSOLIDATED, MEMORY_RETRIEVED, MEMORY_STORED

logger = logging.getLogger(__name__)

# memory:recall 返回的检索来源统计键（供前端展示）
_RECALL_SOURCE_KEYS = ("l1", "l2", "l3")


class MemoryOrchestrator:
    """记忆调度官。"""

    name = "memory"

    def __init__(self, event_bus, db_path: Optional[str] = None,
                 config_loader=None, switch_check: Optional[Callable[[str], bool]] = None,
                 summarize_fn: Optional[Callable[[str, int], str]] = None,
                 strength_provider: Optional[Callable[[], str]] = None):
        self._event_bus = event_bus
        self._config = MemoryConfig(config_loader)
        self._switch_check = switch_check or (lambda name: True)
        self._strength_provider = strength_provider
        self._short = ShortTermMemory()
        self._long = LongTermMemory(db_path=db_path)
        # L2 与 L3 同目录：mid_db = <long_db_dir>/l2.db
        mid_db = None
        if db_path:
            from pathlib import Path
            mid_db = str(Path(db_path).parent / "l2.db")
        self._mid = MidTermMemory(db_path=mid_db)
        self._logger = EventLogger(event_bus, config=self._config)
        self._compressor = MemoryCompressor(
            event_bus, mid_term=self._mid, long_term=self._long, config=self._config,
            summarize_fn=summarize_fn,
            l1_entries_fn=self._logger.l1_entries,
            switch_check=self._switch_check)
        self._reviewer = MemoryReview(switch_check=self._switch_check)
        self._started = False
        registry.bind(self.handle)

    def capabilities(self) -> List[str]:
        return registry.capabilities()

    def set_summarize_fn(self, summarize_fn: Callable[[str, int], str]) -> None:
        """装配层延迟注入 LLM 摘要函数（llm 调度官就绪后调用）。"""
        self._compressor.set_summarize_fn(summarize_fn)

    def start(self) -> None:
        self._logger.start()
        self._compressor.start()
        self._started = True
        logger.info("[MemoryOrchestrator] 已启动（分层记忆：L0/L1/L2/L3 就绪）")

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
        if capability == "memory:recall":
            return self._recall(payload)
        if capability == "memory:compress":
            return self._compress(payload)
        if capability == "memory:review":
            return self._review(payload)
        return {"ok": False, "data": {}, "error": f"unknown capability: {capability}"}

    def health(self) -> Dict[str, Any]:
        return {"status": "ok" if self._started else "down",
                "detail": f"long_term={self._long.count()} mid_term={self._mid.count()} "
                          f"l1_buffer={self._logger.count_l1()}"}

    def stop(self) -> None:
        self._logger.stop()
        self._compressor.stop()
        self._started = False
        self._mid.close()
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

    # ---------- 任务四：分层记忆 ----------

    def _recall(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """分层检索：L1 时间窗 + L2 摘要 + L3 长期，混合打分，纯文本+向量不调 LLM。

        strength 控制 k（低2/中5/高10/超强15，规格书 4.3）。
        """
        query = payload.get("query", "")
        strength = payload.get("strength") or (
            self._strength_provider() if self._strength_provider else "") \
            or self._config.strength_default
        k = self._config.k_for(strength)
        session_id = payload.get("session_id", "default")
        bucket = self._bucket(session_id, payload.get("character_id", ""))
        mode = payload.get("mode", "hybrid")
        l1 = self._logger.l1_entries()
        # L2 摘要条目统一字段：content=summary（与 L1/L3 条目 schema 对齐，前端可直接展示）
        l2 = [dict(e, content=e.get("summary", ""))
              for e in self._mid.get_recent(bucket)]
        l3 = self._long.retrieve(query, k) if query else self._long.get_history(bucket, k)
        candidates = l1 + l2 + l3
        if not candidates:
            source = {key: 0 for key in _RECALL_SOURCE_KEYS}
            return {"ok": True, "data": {"memories": [], "source": source,
                                         "strength": strength, "k": k}, "error": None}
        if query:
            if mode == "vector":
                matched = retriever.vector_retrieve(candidates, query, k)
            elif mode == "keyword":
                matched = retriever.keyword_retrieve(candidates, query, k)
            else:
                matched = retriever.hybrid_retrieve(candidates, query, k)
        else:
            matched = retriever.merge_results([], candidates, k)
        source = {"l1": len(l1), "l2": len(l2), "l3": len(l3)}
        return {"ok": True, "data": {"memories": matched, "source": source,
                                     "strength": strength, "k": k}, "error": None}

    def _compress(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """手动触发压缩：L1→L2 摘要 + L2→L3 归档（受 memory_compression 开关控制）。"""
        if not self._switch_check("memory_compression"):
            return {"ok": False, "data": {}, "error": "memory_compression 开关关闭"}
        session_id = payload.get("session_id", "default")
        bucket = self._bucket(session_id, payload.get("character_id", ""))
        triggered = self._compressor.compress_now(bucket)
        archived = self._compressor.consolidate_to_l3(bucket)
        return {"ok": True, "data": {"triggered": triggered, "archived": archived},
                "error": None}

    def _review(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """世界书提案审批流：action ∈ propose/list/accept/reject。"""
        action = payload.get("action", "list")
        if action == "propose":
            pid = self._reviewer.propose(
                payload.get("source_memory_id", ""),
                payload.get("content", ""),
                payload.get("reason", ""))
            return {"ok": True, "data": {"proposal_id": pid}, "error": None}
        if action == "list":
            return {"ok": True,
                    "data": {"proposals": self._reviewer.list(payload.get("status"))},
                    "error": None}
        if action == "accept":
            ok = self._reviewer.accept(payload.get("proposal_id", ""))
            return {"ok": ok, "data": {"accepted": ok},
                    "error": None if ok else "提案不存在或已处置"}
        if action == "reject":
            ok = self._reviewer.reject(payload.get("proposal_id", ""),
                                       payload.get("reason", ""))
            return {"ok": ok, "data": {"rejected": ok},
                    "error": None if ok else "提案不存在或已处置"}
        return {"ok": False, "data": {}, "error": f"unknown review action: {action}"}
