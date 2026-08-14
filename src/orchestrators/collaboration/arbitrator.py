"""arbitrator.py — 发言权仲裁器（核心）。

arbitrate(source, text, ...) → 依次应用规则链；当前有人发言时请求入待发队列，
verdict.deferred=True 表示"排队待发"，False 表示放行或无人回应。
发布 speech:arbitrated（role/rule_hit/request_id/deferred）供前端展示与调试。
零 LLM：规则全部为确定性文本规则（rules.py）。
"""
import logging
import uuid
from dataclasses import dataclass
from typing import List, Optional

from src.orchestrators.collaboration.rules import (
    ArbitrationContext, Rule, build_default_rules, make_rules_by_order,
)
from src.orchestrators.collaboration.turn_tracker import TurnTracker
from src.shared.events import SPEECH_ARBITRATED

logger = logging.getLogger(__name__)


@dataclass
class ArbitrationVerdict:
    role: Optional[str]
    rule_hit: str = ""
    request_id: str = ""
    deferred: bool = False          # True=排队待发（互斥忙）；False=放行或无人回应


class SpeakerArbitrator:
    def __init__(self, event_bus, turn_tracker: Optional[TurnTracker] = None,
                 profiles=None, lead_role: str = "yuki",
                 rules_order: Optional[List[str]] = None,
                 present_roles: Optional[set] = None,
                 seed: Optional[int] = None):
        self._event_bus = event_bus
        self._tt = turn_tracker or TurnTracker()
        self._profiles = profiles
        self._lead_role = lead_role
        # 在场模型单一来源（ADR-001，会话状态归指挥官）：注入 session.present_roles
        # 副本；None 表示未注入，_present_roles() 回退 profiles.all_roles()/默认双人组。
        self._present = (set(present_roles) if present_roles is not None else None)
        self._rules: List[Rule] = (make_rules_by_order(rules_order, seed=seed)
                                   if rules_order else build_default_rules(seed=seed))

    def set_profiles(self, profiles) -> None:
        self._profiles = profiles

    def set_present_roles(self, present_roles) -> None:
        """注入在场角色集合（session 单源同步；coordinator 订阅 presence_changed 时调用）。

        None 表示恢复未注入状态（回退 profiles.all_roles()）；空集即空集（保留）。
        """
        self._present = (set(present_roles) if present_roles is not None else None)

    def set_lead_role(self, role: str) -> None:
        self._lead_role = role

    def arbitrate(self, source: str, text: str, user_name: str = "",
                  kind: str = "danmaku", requester_role: str = "",
                  ref_text: str = "") -> ArbitrationVerdict:
        request_id = uuid.uuid4().hex[:8]
        present = self._present_roles()
        ctx = ArbitrationContext(text=text, user_name=user_name, source=source,
                                 kind=kind, lead_role=self._lead_role,
                                 present_roles=present, profiles=self._profiles,
                                 turn_tracker=self._tt)
        winner = None
        hit = ""
        for rule in self._rules:
            verdict = rule.evaluate(ctx)
            if verdict.role is not None:
                winner = verdict.role
                hit = verdict.reason
                break
        if winner is None:
            return ArbitrationVerdict(None, hit, request_id, deferred=False)

        request = {"role": winner, "priority": self._priority(kind, hit),
                   "request_id": request_id, "text": text, "ref_text": ref_text}
        if not self._tt.acquire(winner):
            self._tt.enqueue(request)     # 有人正发言：入队等待
            self._publish(winner, hit, request_id, deferred=True)
            return ArbitrationVerdict(None, hit, request_id, deferred=True)
        self._publish(winner, hit, request_id, deferred=False)
        return ArbitrationVerdict(winner, hit, request_id, deferred=False)

    @staticmethod
    def _priority(kind: str, hit: str) -> int:
        if hit.startswith("mention"):
            return 0
        if hit.startswith("intent") or hit.startswith("command"):
            return 1
        if hit.startswith("relevance"):
            return 2
        if kind == "collab":
            return 3
        if kind == "active":
            return 4
        return 5

    def _present_roles(self) -> set:
        # 注入值优先（session 单源）；未注入时回退 profiles.all_roles()，再回退默认双人组
        if self._present is not None:
            return set(self._present)
        if self._profiles is not None and hasattr(self._profiles, "all_roles"):
            return set(self._profiles.all_roles())
        return {"yuki", "lilith"}

    def _publish(self, role: str, rule_hit: str, request_id: str,
                 deferred: bool) -> None:
        try:
            self._event_bus.publish(SPEECH_ARBITRATED, role=role,
                                    rule_hit=rule_hit, request_id=request_id,
                                    deferred=deferred)
        except Exception as e:
            logger.warning("[Arbitrator] 发布仲裁事件失败: %s", e)
