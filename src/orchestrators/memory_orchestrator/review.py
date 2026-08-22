"""review.py — 记忆审阅（任务四：L3→世界书改动提案审批流）

从长期记忆提炼「世界书改动提案」，走人工审阅流（接受/驳回），持久化到
data/memory_review.json（任务五消费：登录可见可处理）。受开关
allow_memory_to_worldbook 控制（默认 off：不生成提案，关=不写世界书）。

# 模块内容清单 — review

## 1. 模块身份标识
- 所属调度官：memory（记忆调度官）
- 能力名：memory:review 的实现（propose / list / accept / reject）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| data_file | 否 | data/memory_review.json | str | 提案持久化文件 |
| switch_check | 否 | 恒 True | Callable[[str],bool] | allow_memory_to_worldbook 开关 |

## 3. 输入契约
- 输入格式：`MemoryReview(data_file=None, switch_check=None)`
- 输入格式：`propose(source_memory_id, proposed_content, reason="") -> Optional[str]`
- 输入格式：`list(status=None) -> List[dict]` / `get(proposal_id) -> Optional[dict]`
- 输入格式：`accept(proposal_id) -> bool` / `reject(proposal_id, reason="") -> bool`

## 4. 输出契约
- 成功：propose 返回提案 id（proposal{seq}）；accept/reject 返回 True 并持久化
- 失败：开关关 → propose 返回 None（不生成提案）；未知 id → accept/reject 返回 False
- 事件：无

## 5. 依赖声明
- 外部服务：无
- 内部模块：src.shared.config_loader（PROJECT_ROOT，路径缺省）
- 预先配置：无（数据文件缺省自动创建）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 无 | 文件读写异常记录日志，不中断调用 | 检查 data/ 目录权限 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| propose() | 是 | 生成待审阅提案（受开关控制 + pending 去重） |
| list() / get() | 是 | 查询提案 |
| accept() / reject() | 是 | 审阅处置并持久化 |

## 8. 领域状态说明
- 状态项：_proposals（内存列表，状态 pending/accepted/rejected）
- 持久化：data/memory_review.json（读写全量快照）
- 恢复：构造时读取磁盘恢复全部提案
"""
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.shared.config_loader import PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_REVIEW_FILE = PROJECT_ROOT / "data" / "memory_review.json"


class MemoryReview:
    """世界书改动提案审批流（任务五前端消费）。"""

    def __init__(self, data_file: Optional[str] = None,
                 switch_check: Optional[Callable[[str], bool]] = None):
        self._file = Path(data_file) if data_file else DEFAULT_REVIEW_FILE
        self._switch_check = switch_check or (lambda name: True)
        self._lock = threading.RLock()
        self._proposals: List[Dict[str, Any]] = []
        self._seq = 0
        self._load()

    # ---------- 提案生成 ----------

    def propose(self, source_memory_id: str, proposed_content: str,
                reason: str = "") -> Optional[str]:
        """从长期记忆生成世界书改动提案。

        开关关（allow_memory_to_worldbook=off）→ 返回 None 不生成；
        同来源 + 同内容已有 pending 提案 → 返回已有 id（去重）。
        """
        if not self._switch_check("allow_memory_to_worldbook"):
            return None
        proposed_content = (proposed_content or "").strip()
        if not proposed_content:
            return None
        with self._lock:
            for p in self._proposals:
                if (p["status"] == "pending"
                        and p["source_memory_id"] == source_memory_id
                        and p["proposed_content"] == proposed_content):
                    return p["proposal_id"]
            self._seq += 1
            proposal = {
                "proposal_id": f"proposal{self._seq}",
                "created_at": round(time.time(), 3),
                "source_memory_id": source_memory_id,
                "proposed_content": proposed_content,
                "reason": reason or "",
                "status": "pending",
                "resolved_at": None,
            }
            self._proposals.append(proposal)
            self._save()
            logger.info("[MemoryReview] 新提案 %s（来源 %s）",
                        proposal["proposal_id"], source_memory_id)
            return proposal["proposal_id"]

    # ---------- 查询 ----------

    def list(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._proposals)
        if status:
            items = [p for p in items if p["status"] == status]
        items.sort(key=lambda p: p["created_at"], reverse=True)
        return items

    def get(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for p in self._proposals:
                if p["proposal_id"] == proposal_id:
                    return dict(p)
        return None

    def count(self, status: Optional[str] = None) -> int:
        return len(self.list(status=status))

    # ---------- 审阅处置 ----------

    def accept(self, proposal_id: str) -> bool:
        """接受提案（任务五：接受后由人工写入世界书，本模块只记录状态）。"""
        return self._resolve(proposal_id, "accepted", "")

    def reject(self, proposal_id: str, reason: str = "") -> bool:
        return self._resolve(proposal_id, "rejected", reason)

    def _resolve(self, proposal_id: str, status: str, reason: str) -> bool:
        with self._lock:
            for p in self._proposals:
                if p["proposal_id"] != proposal_id:
                    continue
                if p["status"] != "pending":
                    return False  # 已处置过的提案不可重复处置
                p["status"] = status
                p["resolved_at"] = round(time.time(), 3)
                if reason:
                    p["reason"] = reason
                self._save()
                logger.info("[MemoryReview] 提案 %s → %s", proposal_id, status)
                return True
        return False

    # ---------- 持久化 ----------

    def _load(self) -> None:
        try:
            if not self._file.exists():
                return
            data = json.loads(self._file.read_text(encoding="utf-8"))
            self._proposals = data.get("proposals", [])
            self._seq = data.get("seq", 0)
        except (OSError, ValueError) as e:  # pragma: no cover - 防御
            logger.warning("[MemoryReview] 读取失败（忽略，重建）: %s", e)
            self._proposals = []
            self._seq = 0

    def _save(self) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            payload = {"seq": self._seq, "proposals": self._proposals}
            self._file.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except OSError as e:  # pragma: no cover - 防御
            logger.error("[MemoryReview] 持久化失败: %s", e)
