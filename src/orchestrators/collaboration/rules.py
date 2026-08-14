"""rules.py — 发言权仲裁规则集（可插拔）。

优先级链（config collaboration.rules_order 可调）：
mention(@指定,硬放行) > intent(系统意图→lead) > relevance(关键词加权) >
cooldown(闲置最久) > random(平局随机 + 记录取反)。
零 LLM：全部为确定性文本规则。
"""
import random
import re
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class RuleVerdict:
    role: Optional[str]
    confidence: float
    reason: str


@dataclass
class ArbitrationContext:
    text: str
    user_name: str
    source: str
    kind: str                 # danmaku / collab / active
    lead_role: str
    present_roles: set
    profiles: object          # CharacterProfileLoader 兼容接口（keywords_for）
    turn_tracker: object      # idle_seconds(role) -> float


class Rule:
    name = "base"

    def evaluate(self, ctx: ArbitrationContext) -> RuleVerdict:
        raise NotImplementedError


_MENTION_RE = re.compile(r"@(yuki|lilith)|(yuki|yuki酱|lilith|莉莉丝)\s*(?:你看|你怎么看|你同意|讲|说|来)", re.IGNORECASE)
_INTENT_WORDS = {"下播", "开播", "状态", "感谢", "点歌", "日程", "晚安", "再见"}
_MENTION_LANG = {"yuki": {"yuki", "yuki酱"}, "lilith": {"lilith", "莉莉丝"}}


class MentionRule(Rule):
    """手动指定（硬放行）：@角色 或 "角色+你看/怎么说" 显式指向。"""
    name = "mention"

    def evaluate(self, ctx):
        low = ctx.text.lower()
        m = _MENTION_RE.search(ctx.text)
        if not m:
            return RuleVerdict(None, 0.0, "no-mention")
        hit = None
        for role in ctx.present_roles:
            for alias in _MENTION_LANG.get(role, {role}):
                if alias.lower() in low:
                    hit = role
                    break
            if hit:
                break
        if hit is None:
            return RuleVerdict(None, 0.0, "mention-unknown")
        return RuleVerdict(hit, 1.0, f"mention:{hit}")


class IntentRule(Rule):
    """系统/运营意图 → lead_role。"""
    name = "intent"

    def evaluate(self, ctx):
        low = ctx.text.strip().lower()
        if low.startswith("!"):
            return RuleVerdict(ctx.lead_role, 0.9, f"command:{low}")
        for w in _INTENT_WORDS:
            if w in ctx.text:
                return RuleVerdict(ctx.lead_role, 0.8, f"intent:{w}")
        return RuleVerdict(None, 0.0, "no-intent")


class RelevanceRule(Rule):
    """相关性：patterns > topics > personality 加权。"""
    name = "relevance"

    def evaluate(self, ctx):
        scores: Dict[str, float] = {}
        for role in ctx.present_roles:
            kw = ctx.profiles.keywords_for(role) or {}
            score = 0.0
            for pat in kw.get("patterns", []):
                if pat.startswith("regex:"):
                    if re.search(pat[len("regex:"):], ctx.text):
                        score += 3.0
                elif pat in ctx.text:
                    score += 3.0
            for topic in kw.get("topics", []):
                if topic and topic in ctx.text:
                    score += 2.0
            for tag in kw.get("personality", []):
                if tag and tag in ctx.text:
                    score += 1.0
            scores[role] = score
        top = max(scores.values(), default=0.0)
        if top <= 0:
            return RuleVerdict(None, 0.0, "no-keyword-hit")
        winners = [r for r, s in scores.items() if s == top]
        if len(winners) > 1:
            return RuleVerdict(None, top, "tie")
        return RuleVerdict(winners[0], top, f"relevance:{winners[0]}")


class CooldownRule(Rule):
    """冷却：谁闲置最久谁先说话。"""
    name = "cooldown"

    def evaluate(self, ctx):
        idle = {r: ctx.turn_tracker.idle_seconds(r) for r in ctx.present_roles}
        if not idle:
            return RuleVerdict(None, 0.0, "no-role")
        max_idle = max(idle.values())
        if max_idle <= 0:
            return RuleVerdict(None, 0.0, "all-hot")
        winners = [r for r, v in idle.items() if v == max_idle]
        return RuleVerdict(winners[0], 0.6, f"cooldown:{winners[0]}")


class RandomRule(Rule):
    """随机扰动：平局兜底；记录上次选择，下次偏向另一角色。"""
    name = "random"

    def __init__(self, seed: Optional[int] = None):
        self._rng = random.Random(seed)
        self.last_choice: Optional[str] = None

    def evaluate(self, ctx):
        roles = sorted(ctx.present_roles)
        if not roles:
            return RuleVerdict(None, 0.0, "no-role")
        if self.last_choice and len(roles) > 1:
            others = [r for r in roles if r != self.last_choice]
            choice = self._rng.choice(others)
        else:
            choice = self._rng.choice(roles)
        self.last_choice = choice
        return RuleVerdict(choice, 0.5, f"random:{choice}")


def build_default_rules(seed: Optional[int] = None) -> List[Rule]:
    return [MentionRule(), IntentRule(), RelevanceRule(),
            CooldownRule(), RandomRule(seed=seed)]


def make_rules_by_order(names: List[str]) -> List[Rule]:
    pool = {r.name: r for r in build_default_rules()}
    return [pool[n] for n in names if n in pool]
