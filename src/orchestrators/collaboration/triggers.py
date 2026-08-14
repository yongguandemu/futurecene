"""triggers.py — 联动触发条件：发言完成后决定是否让另一角色接话（banter）。

产出 TriggerProposal → coordinator.request_utterance → 回仲裁器（冷却/互斥约束下放行）。
"""
import logging
import random
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class CollabTriggers:
    def __init__(self, probability: float = 0.3, global_cooldown: float = 20.0,
                 present_roles=None, seed: Optional[int] = None):
        self._probability = float(probability)
        self._cooldown = float(global_cooldown)
        self._present = set(present_roles or {"yuki", "lilith"})
        self._rng = random.Random(seed)
        self._last_trigger_at = 0.0

    def update_runtime(self, probability: float, global_cooldown: float,
                       present_roles=None) -> None:
        self._probability = float(probability)
        self._cooldown = float(global_cooldown)
        if present_roles:
            self._present = set(present_roles)

    def evaluate(self, speaker: str, text: str) -> List[Dict[str, str]]:
        """发言完成后调用；返回接话提案列表（通常 0 或 1 条）。"""
        now = time.time()
        if now - self._last_trigger_at < self._cooldown:
            return []
        if self._probability <= 0 or self._rng.random() > self._probability:
            return []
        others = [r for r in sorted(self._present) if r != speaker]
        if not others:
            return []
        self._last_trigger_at = now
        target = others[0]
        return [{"role": target, "kind": "banter", "reason": "speech-completed",
                 "ref_text": text}]
