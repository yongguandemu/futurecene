"""experience_store.py — 经验案例库（游戏经验学习域）

持久化「状态→动作→结果」三元组。置信度 = 成功次数 / 总次数；
相似状态检索；复合技能沉淀（Voyager 式）；跨会话累积（本地 json）。

# 模块内容清单 — experience_store

## 1. 模块身份标识
- 所属调度官：experience
- 能力名：experience:record / experience:query / experience:stats

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| data_file | 否 | data/experience/{game}.json | str | 经验数据文件路径 |
| game | 否 | "default" | str | 游戏标识（决定默认文件路径） |
| min_confidence | 否 | 0.7 | float，0.0-1.0 | 检索最低置信度阈值 |
| query_top_k | 否 | 3 | int，>=1 | 相似检索返回最多条数 |
| max_entries | 否 | 5000 | int，>=1 | 内存最大条目数（超限淘汰最旧） |
| save_throttle | 否 | 5.0 | float，>0 | 落盘节流间隔（秒） |

## 3. 输入契约
- 输入格式：`record(state, action, args, outcome)` / `query(state, min_confidence)` / `record_skill(skill_name, condition, steps, success)` / `query_skill(condition, min_confidence)` / `reset()` / `flush()` / `stats()`
- state：GameState，状态快照
- action：str，执行的动作名
- args：dict，动作参数
- outcome：str ∈ {success, failure, no_change}
- condition：dict，{type: str, ...}
- steps：list[{action, args}...]

## 4. 输出契约
- 成功：`record()/record_skill()/flush()` 返回 `None`；`query()` 返回 `[(rec, sim), ...]`；`query_skill()` 返回 `[(rec, score), ...]`；`reset()` 返回 int（清空条数）；`stats()` 返回 dict（entries/high_confidence/file）
- 失败：`reset()` 正常返回条数；异常时静默处理
- 事件：无（数据由 learn_brain 发布事件）

## 5. 依赖声明
- 外部服务：本地文件系统（json 读写）
- 内部模块：`state_encoder.GameState`、`state_encoder.StateEncoder`、`src/shared/config_loader.PROJECT_ROOT`
- 预先配置：无（首次运行自动创建目录和文件）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 加载失败 | json 文件损坏或不存在 | 清空 entries，记录警告 |
| 写入失败 | 文件系统权限不足 | 记录警告，跳过落盘 |
| 条目超限 | 超过 max_entries | 淘汰最旧条目后添加新条目 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start | 否 | 构造即加载（_load 从文件恢复） |
| stop | 否 | 由 learn_brain stop 时调用 flush 落盘 |

## 8. 领域状态说明
- 状态项：`_entries`（key → 经验记录 dict，含 success_count/fail_count/confidence）、`_dirty`（脏标记）、`_last_save`（上次保存时间）
- 持久化：本地 json 文件，跨会话累积
- 恢复：构造时从 data_file 加载；flush 强制落盘
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