"""experience_store.py — 经验案例库（游戏经验学习域）

持久化「状态→动作→结果」三元组。置信度 = 成功次数 / 总次数；
相似状态检索；复合技能沉淀（Voyager 式）；跨会话累积（本地 json）。

# 模块内容清单（8 项契约摘录）
- 所属调度官：experience
- 能力名：experience:record / experience:query / experience:stats
- 配置契约：data_file(默认 data/experience/{game}.json) / min_confidence(0.7) / query_top_k(3) / max_entries(5000)
- 输入契约：record(state, action, args, outcome)；query(state, min_confidence)
- 输出契约：query 返回 [(rec, sim), ...]；stats 返回条目摘要
- 生命周期：flush()（落盘）；领域状态：内存条目 + 脏标记，重启从 json 恢复
"""
import json
import os
import threading
import time
import logging

from src.orchestrators.experience_orchestrator.state_encoder import GameState, StateEncoder
from src.shared.config_loader import PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = PROJECT_ROOT / "data" / "experience"


class ExperienceStore:
    """状态-动作-结果三元组持久化与相似检索。"""

    def __init__(self, data_file: str = None, game: str = "default",
                 min_confidence: float = 0.7, query_top_k: int = 3,
                 max_entries: int = 5000, save_throttle: float = 5.0):
        self._file = data_file or self._default_file(game)
        self.min_confidence = min_confidence
        self.top_k = query_top_k
        self.max_entries = max_entries
        self.save_throttle = save_throttle
        self._entries = {}
        self._lock = threading.Lock()
        self._dirty = False
        self._last_save = 0.0
        self._load()

    @staticmethod
    def _default_file(game: str) -> str:
        return str(DEFAULT_DATA_DIR / "{}.json".format(game or "default"))

    def _load(self):
        try:
            if os.path.exists(self._file):
                with open(self._file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._entries = data.get("entries", {})
                logger.info("[Experience] 已加载经验 %s 条", len(self._entries))
        except Exception as e:
            logger.warning("[Experience] 加载失败: %s", e)
            self._entries = {}

    def _save(self):
        with self._lock:
            if not self._dirty:
                return
            if time.time() - self._last_save < self.save_throttle:
                return
            os.makedirs(os.path.dirname(self._file), exist_ok=True)
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump({"entries": self._entries}, f, ensure_ascii=False)
            self._dirty = False
            self._last_save = time.time()

    def flush(self):
        """强制落盘（stop 时调用）。"""
        with self._lock:
            self._dirty = True
        self._save()
        with self._lock:
            self._dirty = False
            self._last_save = 0.0
        self._save()

    @staticmethod
    def _key(state: GameState, action: str, args: dict) -> str:
        return "{}|{}|{}".format(state.fingerprint, action,
                                 json.dumps(args, sort_keys=True, ensure_ascii=False))

    def record(self, state: GameState, action: str, args: dict,
               outcome: str = "no_change"):
        """回写一条经验。outcome ∈ {success, failure, no_change}。"""
        args = args or {}
        key = self._key(state, action, args)
        with self._lock:
            rec = self._entries.get(key)
            if rec is None:
                if len(self._entries) >= self.max_entries:
                    oldest = min(self._entries.items(),
                                 key=lambda kv: kv[1].get("last_used", 0))
                    del self._entries[oldest[0]]
                rec = {"state": state.to_dict(), "action": action, "args": args,
                       "success_count": 0, "fail_count": 0, "last_used": time.time(),
                       "confidence": 0.0}
                self._entries[key] = rec
            if outcome == "success":
                rec["success_count"] += 1
            elif outcome == "failure":
                rec["fail_count"] += 1
            rec["last_used"] = time.time()
            total = rec["success_count"] + rec["fail_count"]
            rec["confidence"] = rec["success_count"] / total if total else 0.0
            self._dirty = True
        self._save()

    def query(self, state: GameState, min_confidence: float = None) -> list:
        """按相似状态检索经验。返回 [(rec, sim), ...]。"""
        mc = min_confidence if min_confidence is not None else self.min_confidence
        with self._lock:
            cands = []
            for rec in self._entries.values():
                if rec.get("kind") == "skill":
                    continue
                try:
                    rs = GameState.from_dict(rec["state"])
                    sim = StateEncoder.similarity(state, rs)
                    if sim >= 0.5 and rec.get("confidence", 0.0) >= mc:
                        cands.append((rec, sim))
                except Exception:
                    continue
        cands.sort(key=lambda x: x[1], reverse=True)
        return cands[:self.top_k]

    def record_skill(self, skill_name: str, condition: dict, steps: list,
                     success: bool = True):
        """沉淀/更新复合技能。condition: {type, target}；steps: [{action, args}...]。"""
        with self._lock:
            key = "skill|{}|{}".format(skill_name, json.dumps(condition, sort_keys=True))
            rec = self._entries.get(key)
            if rec is None:
                rec = {"kind": "skill", "skill": skill_name, "condition": condition,
                       "steps": steps, "success_count": 0, "fail_count": 0,
                       "last_used": time.time(), "confidence": 0.0}
                self._entries[key] = rec
            if success:
                rec["success_count"] += 1
            else:
                rec["fail_count"] += 1
            rec["steps"] = steps
            rec["last_used"] = time.time()
            total = rec["success_count"] + rec["fail_count"]
            rec["confidence"] = rec["success_count"] / total if total else 0.0
            self._dirty = True
        self._save()

    def query_skill(self, condition: dict, min_confidence: float = None) -> list:
        """按条件检索复合技能。返回 [(rec, score), ...]。"""
        mc = min_confidence if min_confidence is not None else self.min_confidence
        with self._lock:
            out = []
            for rec in self._entries.values():
                if rec.get("kind") != "skill":
                    continue
                if rec.get("confidence", 0.0) < mc:
                    continue
                cond = rec.get("condition") or {}
                score = 1.0 if cond.get("type") == condition.get("type") else 0.0
                if score:
                    out.append((rec, score))
        out.sort(key=lambda x: x[1], reverse=True)
        return out[:3]

    def reset(self) -> int:
        with self._lock:
            n = len(self._entries)
            self._entries = {}
            self._dirty = True
        self.flush()
        logger.info("[Experience] 已清空 %s 条经验", n)
        return n

    def stats(self) -> dict:
        with self._lock:
            succ = sum(1 for r in self._entries.values() if r["confidence"] >= 0.7)
            return {"entries": len(self._entries), "high_confidence": succ,
                    "file": self._file}