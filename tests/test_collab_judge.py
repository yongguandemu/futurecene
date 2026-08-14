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


class FakeLLM:
    """可控 LLM：按注入回复序列返回；可配置抛错。"""

    def __init__(self, replies=None, error=None):
        self._replies = list(replies or [])
        self._error = error
        self.calls = 0

    def _chat(self, payload):
        self.calls += 1
        if self._error:
            raise self._error
        if self._replies:
            reply = self._replies.pop(0)
        else:
            reply = '{"yuki": 0.2, "lilith": 0.8, "silent": false}'
        return {"ok": True, "data": {"reply": reply}}


class FakeProfilesWithLoad(FakeProfiles):
    def load(self, role):
        return type("P", (), {"system_prompt": f"{role} 的画像"})() if role in (
            "yuki", "lilith") else None


def _judge_ctx(text="随便聊聊"):
    ctx = _ctx(text)
    ctx.profiles = FakeProfilesWithLoad()
    return ctx


def test_llm_judge_parses_urgencies():
    from src.orchestrators.collaboration.judge import LLMJudge
    j = LLMJudge(FakeLLM(), FakeProfilesWithLoad(), budget_per_min=10,
                 rules_order=["random"], rng_seed=1)
    r = j.judge(_judge_ctx())
    assert r.urgencies["lilith"] == 0.8 and r.silent is False and r.source == "llm"


def test_llm_judge_silent_true():
    from src.orchestrators.collaboration.judge import LLMJudge
    llm = FakeLLM(replies=['{"yuki": 0.1, "lilith": 0.1, "silent": true}'])
    j = LLMJudge(llm, FakeProfilesWithLoad(), budget_per_min=10,
                 rules_order=["random"], rng_seed=1)
    r = j.judge(_judge_ctx())
    assert r.silent is True


def test_llm_judge_fallback_on_error():
    from src.orchestrators.collaboration.judge import LLMJudge
    j = LLMJudge(FakeLLM(error=RuntimeError("boom")), FakeProfilesWithLoad(),
                 budget_per_min=10, rules_order=["random"], rng_seed=1)
    r = j.judge(_judge_ctx())
    assert r.source == "rules-fallback" and r.silent is False


def test_llm_judge_fallback_on_bad_json():
    from src.orchestrators.collaboration.judge import LLMJudge
    j = LLMJudge(FakeLLM(replies=["这不是 JSON"]), FakeProfilesWithLoad(),
                 budget_per_min=10, rules_order=["random"], rng_seed=1)
    r = j.judge(_judge_ctx())
    assert r.source == "rules-fallback"


def test_llm_judge_budget_exhausted_falls_back():
    from src.orchestrators.collaboration.judge import LLMJudge
    llm = FakeLLM()
    j = LLMJudge(llm, FakeProfilesWithLoad(), budget_per_min=2,
                 rules_order=["random"], rng_seed=1)
    j.judge(_judge_ctx())
    j.judge(_judge_ctx())
    r = j.judge(_judge_ctx())   # 第 3 次：预算耗尽
    assert r.source == "rules-fallback"
    assert llm.calls == 2       # 未发起第 3 次 LLM 调用
