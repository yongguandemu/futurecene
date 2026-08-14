"""rules.py — 发言权仲裁规则集（可插拔）。

优先级链（config collaboration.rules_order 可调）：
mention(@指定,硬放行) > intent(系统意图→lead) > relevance(关键词加权) >
cooldown(闲置最久) > random(平局随机 + 记录取反)。
零 LLM：全部为确定性文本规则。
"""
import logging
import random
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


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


_INTENT_WORDS = {"下播", "开播", "状态", "感谢", "点歌", "日程", "晚安", "再见"}
_MENTION_LANG = {"yuki": {"yuki", "yuki酱"}, "lilith": {"lilith", "莉莉丝"}}
_MENTION_VERBS = ("你看", "你怎么看", "你同意", "讲", "说", "来")
# 中文没有 \b 边界：前缀/后缀均用 (?<![A-Za-z0-9_]) / (?![A-Za-z0-9_])
# 阻断 "yukiko"/"yuki_ai"/"yuki123" 等 ASCII 词字符粘连（注意 Python re 的
# \w 会匹配中文，故不能用 (?!\w)，否则 "yuki你看" 这类中文紧随会被误拒）。
_ASCII_BOUND = r"(?<![A-Za-z0-9_])"
_END_BOUND = r"(?![A-Za-z0-9_])"


def _build_mention_patterns():
    """由 _MENTION_LANG 动态生成 [(role, compiled_re)]；新增角色只需扩展 _MENTION_LANG。"""
    patterns = []
    for role, aliases in _MENTION_LANG.items():
        # 长别名优先（yuki酱 先于 yuki），保证整体匹配
        esc = sorted({re.escape(a) for a in aliases}, key=len, reverse=True)
        alt = "|".join(esc)
        patterns.append(
            (role, re.compile(rf"@{_ASCII_BOUND}(?:{alt}){_END_BOUND}", re.IGNORECASE)))
        verbs = "|".join(re.escape(v) for v in _MENTION_VERBS)
        patterns.append(
            (role, re.compile(rf"{_ASCII_BOUND}(?:{alt}){_END_BOUND}\s*(?:{verbs})", re.IGNORECASE)))
    return patterns


_MENTION_PATTERNS = _build_mention_patterns()


class MentionRule(Rule):
    """手动指定（硬放行）：@角色 或 "角色+你看/怎么说" 显式指向。"""
    name = "mention"

    def evaluate(self, ctx):
        # 按命中区间起点排序取最早命中（而非全局子串查找），
        # 保证 "@lilith 你看 yuki酱" 归 lilith 而非文本中出现的 yuki酱
        hits = []
        for role, pat in _MENTION_PATTERNS:
            m = pat.search(ctx.text)
            if m:
                hits.append((m.start(), role))
        if not hits:
            return RuleVerdict(None, 0.0, "no-mention")
        hits.sort()
        # 规格 §8.1 不在场语义（终审 M1）：显式命中的角色不在场 → 返回 None
        # 不转派其它角色；同时命中多个角色 → 取第一个在场者（按命中位置）；
        # 全部命中角色不在场 → mention-not-present（区别于 no-mention）。
        for _, role in hits:
            if role in ctx.present_roles:
                return RuleVerdict(role, 1.0, f"mention:{role}")
        return RuleVerdict(None, 0.0, "mention-not-present")


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
                if not pat:
                    continue
                if pat.startswith("regex:"):
                    try:
                        if re.search(pat[len("regex:"):], ctx.text):
                            score += 3.0
                    except re.error as err:
                        logger.warning("relevance: 非法正则 %r 已跳过: %s", pat, err)
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
        if len(winners) > 1:
            # 平局回退：交给链尾 RandomRule（与 RelevanceRule.tie 语义一致，
            # 不再依赖 set 迭代序随机选人）
            return RuleVerdict(None, 0.6, "tie")
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


def make_rules_by_order(names: List[str], seed: Optional[int] = None) -> List[Rule]:
    """按名称顺序组装规则；seed 透传给 RandomRule（同 seed 重复调用行为一致），
    未知名记 warning 并跳过。"""
    pool = {r.name: r for r in build_default_rules(seed=seed)}
    rules = []
    for n in names:
        if n in pool:
            rules.append(pool[n])
        else:
            logger.warning("make_rules_by_order: 未知规则名 %r 已跳过", n)
    return rules
