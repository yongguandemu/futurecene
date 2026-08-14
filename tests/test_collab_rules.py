"""仲裁规则单测（零 LLM，mock turn_tracker）。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.collaboration.rules import (
    ArbitrationContext, MentionRule, IntentRule, RelevanceRule,
    CooldownRule, RandomRule,
)


class FakeTT:
    def __init__(self, last=None):
        self.last = last or {"yuki": 100.0, "lilith": 50.0}  # yuki 更久未说

    def idle_seconds(self, role):
        return self.last.get(role, 0.0)


class FakeProfiles:
    def keywords_for(self, role):
        return {"yuki": {"topics": ["故事", "月亮"], "patterns": ["讲个故事"]},
                "lilith": {"topics": ["吐槽", "直播"], "patterns": []}}[role]


def _ctx(text, lead="yuki", kind="danmaku"):
    return ArbitrationContext(text=text, user_name="观众", source="danmaku",
                              kind=kind, lead_role=lead,
                              present_roles={"yuki", "lilith"},
                              profiles=FakeProfiles(), turn_tracker=FakeTT())


def test_mention_rule():
    r = MentionRule()
    assert r.evaluate(_ctx("@Lilith 你同意吗")).role == "lilith"
    assert r.evaluate(_ctx("Lilith你怎么看")).role == "lilith"
    assert r.evaluate(_ctx("Yuki酱讲个故事")).role == "yuki"


def test_intent_rule_routes_to_lead():
    r = IntentRule()
    assert r.evaluate(_ctx("下播", lead="yuki")).role == "yuki"
    assert r.evaluate(_ctx("!状态", lead="lilith")).role == "lilith"


def test_relevance_rule():
    r = RelevanceRule()
    assert r.evaluate(_ctx("Yuki讲个笑话")).role is None  # 无关键词命中
    assert r.evaluate(_ctx("讲个故事吧")).role == "yuki"


def test_cooldown_rule_prefers_idle():
    r = CooldownRule()
    assert r.evaluate(_ctx("随便聊聊")).role == "yuki"  # yuki 闲置更久


def test_random_rule_never_none():
    r = RandomRule(seed=1)
    verdict = r.evaluate(_ctx("随便聊聊"))
    assert verdict.role in {"yuki", "lilith"}
