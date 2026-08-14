"""triggers.py — 联动触发条件：发言完成后决定是否让另一角色接话（banter）。

产出 TriggerProposal → coordinator.request_utterance → 回仲裁器（冷却/互斥约束下放行）。

冷却语义：global_cooldown 为"成功产出"后的静默期——只有 evaluate() 实际产出了接话
提案才记录触发时间并进入冷却；概率未命中（rng 判定不通过）不消耗冷却额度。

空集语义：present_roles 显式传空集时即为空集（无在场角色，evaluate 恒返回 []）；
仅在未传（None）时才回退到默认在场名单。

# 模块内容清单 — triggers

## 1. 模块身份标识
- 所属调度官：collaboration（多角色协作域）
- 能力名：collab:trigger（接话触发，间接经 coordinator 调用）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| probability | 否 | 0.3 | float，0.0-1.0 | 接话触发概率 |
| global_cooldown | 否 | 20.0 | float，>0 | 成功产出后的静默期（秒）；概率未命中不消耗 |
| present_roles | 否 | {"yuki","lilith"} | set[str] | 在场角色（显式空集=无在场） |
| seed | 否 | None | int | 随机种子（同 seed 同结果） |

## 3. 输入契约
- 输入格式：`evaluate(speaker, text)` -> list；`update_runtime(probability, global_cooldown, present_roles)`
- speaker：str，刚完成发言的角色；text：str，发言文本

## 4. 输出契约
- 成功：返回接话提案列表（通常 0 或 1 条）：`[{"role", "kind": "banter", "reason": "speech-completed", "ref_text"}]`
- 失败：冷却中 / 概率未命中 / 无在场候选时返回 `[]`（不消耗冷却额度）
- 事件：无（提案由 coordinator 转仲裁）

## 5. 依赖声明
- 外部服务：无
- 内部模块：random、threading、time、typing（纯标准库）
- 预先配置：无

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 无（概率静默） | - | 冷却/概率/候选为空均返回 []，不抛异常 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start/stop | 否 | 无（随 coordinator 生命周期） |
| update_runtime | 是 | 运行时更新触发参数与在场名单 |

## 8. 领域状态说明
- 状态项：`_present`（在场名单）、`_last_trigger_at`（冷却起点）、`_rng`（随机源）、`_probability`、`_cooldown`
- 持久化：无
- 恢复：无（冷却随进程生命周期）
"""
import random
import threading
import time
from typing import Dict, List, Optional

_QUESTION_SUFFIXES = ("？", "?")
_STORY_MARKERS = ("故事", "笑话", "讲个", "哈哈", "好玩", "真的假的")


class CollabTriggers:
    def __init__(self, probability: float = 0.3, global_cooldown: float = 20.0,
                 present_roles=None, seed: Optional[int] = None):
        # 空集就是空集：仅当 present_roles 为 None（未传）时回退默认双人组；
        # 显式传入空集合时保留空集，后续 evaluate 在无在场候选时返回 []。
        self._present = set(present_roles) if present_roles is not None \
            else {"yuki", "lilith"}
        self._probability = float(probability)
        self._cooldown = float(global_cooldown)
        self._rng = random.Random(seed)
        self._last_trigger_at = 0.0
        self._lock = threading.Lock()

    def update_runtime(self, probability: float, global_cooldown: float,
                       present_roles=None) -> None:
        """运行时更新触发参数。

        present_roles 语义（与 __init__ 区分）：
        - None：不更新在场名单（保留当前名单）；
        - 空集：显式清空在场名单（空集就是空集，evaluate 在无候选时返回 []）。
        """
        with self._lock:
            self._probability = float(probability)
            self._cooldown = float(global_cooldown)
            if present_roles is not None:
                self._present = set(present_roles)

    @staticmethod
    def _structural_effective_probability(probability: float, text: str) -> float:
        """结构驱动概率（V2，对话结构信号）：提问必须被接（1.0）；
        故事/玩笑倾向被接（0.6）；否则用配置概率。"""
        t = (text or "").strip()
        if t.endswith(_QUESTION_SUFFIXES):
            return 1.0
        if any(m in t for m in _STORY_MARKERS):
            return max(probability, 0.6)
        return probability

    def evaluate(self, speaker: str, text: str) -> List[Dict[str, str]]:
        """发言完成后调用；返回接话提案列表（通常 0 或 1 条）。

        冷却语义：global_cooldown 是“成功产出”后的静默期——仅当本次实际产出提案时才
        刷新冷却起点；概率未命中（未产出提案）不消耗冷却额度，可立即继续触发。
        结构增强：发言以问号结尾 → 接话概率提升为 1.0（提问必须被接）；
        含故事/玩笑标记 → 提升为 max(配置, 0.6)（倾向被接）。
        目标选择：在场且非 speaker 的候选中用注入的 _rng 随机选取（同 seed 同结果）。
        冷却检查与状态写入由 _lock 保护（与 turn_tracker 风格一致），避免并发竞态。
        """
        now = time.time()
        with self._lock:
            if now - self._last_trigger_at < self._cooldown:
                return []
            effective = self._structural_effective_probability(self._probability, text)
            if effective <= 0 or self._rng.random() > effective:
                return []
            others = [r for r in sorted(self._present) if r != speaker]
            if not others:
                return []
            self._last_trigger_at = now
            target = self._rng.choice(others)
        return [{"role": target, "kind": "banter", "reason": "speech-completed",
                 "ref_text": text}]
