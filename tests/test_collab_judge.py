"""judge 单测（V3：紧迫度协议 + RulesJudge）。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.collaboration.judge import RulesJudge
from src.orchestrators.collaboration.rules import (
    ArbitrationContext,
    make_rules_by_order,
)


class FakeProfiles:
    def keywords_for(self, role):
        return {"yuki": {"topics": ["故事", "月亮"], "patterns": ["讲个故事"]},
                "lilith": {"topics": ["吐槽", "直播"], "patterns": []}}[role]


class FakeTT:
    def __init__(self, last=None):
        self.last = last or {"yuki": 100.0, "lilith": 50.0}

    def idle_seconds(self, role):
        return self.last.get(role, 0.0)

    def turn_history(self, limit=10):
        return []


def _ctx(text):
    return ArbitrationContext(text=text, user_name="观众", source="danmaku",
                              kind="danmaku", lead_role="yuki",
                              present_roles={"yuki", "lilith"},
                              profiles=FakeProfiles(), turn_tracker=FakeTT())


def test_rules_judge_returns_winner_urgency():
    rules = make_rules_by_order(["mention", "relevance", "random"], seed=1)
    r = RulesJudge(rules).judge(_ctx("@Lilith 你怎么看"))
    assert r.urgencies == {"lilith": 1.0}
    assert r.silent is False
    assert r.source == "rules"


def test_rules_judge_matches_chain_semantics():
    # 无 @、无关键词 → 落到链尾 random（与仲裁器既有行为一致，非 silent）
    rules = make_rules_by_order(["mention", "relevance", "random"], seed=1)
    r = RulesJudge(rules).judge(_ctx("随便聊聊"))
    assert r.silent is False
    assert len(r.urgencies) == 1                      # 仅胜者角色有紧迫度
    assert set(r.urgencies) <= {"yuki", "lilith"}
    assert list(r.urgencies.values())[0] == 0.5       # random 规则 confidence


def test_rules_judge_silent_on_empty_present():
    rules = make_rules_by_order(["mention", "random"], seed=1)
    ctx = _ctx("随便聊聊")
    ctx.present_roles = set()
    r = RulesJudge(rules).judge(ctx)
    assert r.silent is True and r.urgencies == {}
