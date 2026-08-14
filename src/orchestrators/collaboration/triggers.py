"""triggers.py — 联动触发条件：发言完成后决定是否让另一角色接话（banter）。

产出 TriggerProposal → coordinator.request_utterance → 回仲裁器（冷却/互斥约束下放行）。

冷却语义：global_cooldown 为“成功产出”后的静默期——只有 evaluate() 实际产出了接话
提案才记录触发时间并进入冷却；概率未命中（rng 判定不通过）不消耗冷却额度。

空集语义：present_roles 显式传空集时即为空集（无在场角色，evaluate 恒返回 []）；
仅在未传（None）时才回退到默认在场名单。
"""
import random
import threading
import time
from typing import Dict, List, Optional


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

    def evaluate(self, speaker: str, text: str) -> List[Dict[str, str]]:
        """发言完成后调用；返回接话提案列表（通常 0 或 1 条）。

        冷却语义：global_cooldown 是“成功产出”后的静默期——仅当本次实际产出提案时才
        刷新冷却起点；概率未命中（未产出提案）不消耗冷却额度，可立即继续触发。
        目标选择：在场且非 speaker 的候选中用注入的 _rng 随机选取（同 seed 同结果）。
        冷却检查与状态写入由 _lock 保护（与 turn_tracker 风格一致），避免并发竞态。
        """
        now = time.time()
        with self._lock:
            if now - self._last_trigger_at < self._cooldown:
                return []
            if self._probability <= 0 or self._rng.random() > self._probability:
                return []
            others = [r for r in sorted(self._present) if r != speaker]
            if not others:
                return []
            self._last_trigger_at = now
            target = self._rng.choice(others)
        return [{"role": target, "kind": "banter", "reason": "speech-completed",
                 "ref_text": text}]
