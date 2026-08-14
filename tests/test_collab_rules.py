"""仲裁规则单测（零 LLM，mock turn_tracker）。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.collaboration.rules import (
    ArbitrationContext, MentionRule, IntentRule, RelevanceRule,
    CooldownRule, RandomRule, ContinuationRule, BalanceRule, make_rules_by_order,
)


class FakeTT:
    def __init__(self, last=None):
        self.last = last or {"yuki": 100.0, "lilith": 50.0}  # yuki 更久未说

    def idle_seconds(self, role):
        return self.last.get(role, 0.0)


class FakeTTWithHistory:
    """带话轮历史的 turn_tracker 桩（结构规则数据源）。"""

    def __init__(self, history=None, last=None):
        self._history = history or []
        self.last = last or {"yuki": 100.0, "lilith": 50.0}

    def idle_seconds(self, role):
        return self.last.get(role, 0.0)

    def turn_history(self, limit=10):
        return list(self._history[-limit:])


class FakeProfiles:
    def keywords_for(self, role):
        return {"yuki": {"topics": ["故事", "月亮"], "patterns": ["讲个故事"]},
                "lilith": {"topics": ["吐槽", "直播"], "patterns": []}}[role]


class BadPatternProfiles:
    """含非法正则与空串 pattern，用于容错测试。"""

    def keywords_for(self, role):
        return {"yuki": {"topics": [], "patterns": ["regex:[", ""]},
                "lilith": {"topics": [], "patterns": []}}[role]


def _ctx(text, lead="yuki", kind="danmaku"):
    return ArbitrationContext(text=text, user_name="观众", source="danmaku",
                              kind=kind, lead_role=lead,
                              present_roles={"yuki", "lilith"},
                              profiles=FakeProfiles(), turn_tracker=FakeTT())


def _ctx_raw(text, profiles=None, tracker=None):
    return ArbitrationContext(text=text, user_name="观众", source="danmaku",
                              kind="danmaku", lead_role="yuki",
                              present_roles={"yuki", "lilith"},
                              profiles=profiles or FakeProfiles(),
                              turn_tracker=tracker or FakeTT())


def test_mention_rule():
    r = MentionRule()
    assert r.evaluate(_ctx("@Lilith 你同意吗")).role == "lilith"
    assert r.evaluate(_ctx("Lilith你怎么看")).role == "lilith"
    assert r.evaluate(_ctx("Yuki酱讲个故事")).role == "yuki"


def test_mention_rule_word_boundary():
    r = MentionRule()
    # 反例：@yukiko 不得因前缀 yuki 误命中（词边界）
    v = r.evaluate(_ctx("@yukiko 讲个故事"))
    assert v.role is None
    assert v.reason == "no-mention"
    # 命中区间判定：@lilith 你看 yuki酱 -> lilith（按正则命中区间而非全局子串）
    assert r.evaluate(_ctx("@lilith 你看 yuki酱")).role == "lilith"


def test_mention_rule_not_present_no_reassign():
    """规格 §8.1（终审 M1）：显式命中的角色不在场 → 返回 None，不转派其它角色。"""
    r = MentionRule()
    # lilith 不在场：@Lilith 显式指向 lilith → 不得转派给在场的 yuki
    ctx = _ctx("@Lilith 你同意吗")
    ctx.present_roles = {"yuki"}
    v = r.evaluate(ctx)
    assert v.role is None
    assert v.reason == "mention-not-present"
    assert v.confidence == 0.0


def test_mention_rule_first_present_wins():
    """规格 §8.1（终审 M1）：同时命中多个角色 → 取第一个在场者（按命中位置）。"""
    r = MentionRule()
    # 双命中且都在场：@lilith 位置最早 → lilith
    assert r.evaluate(_ctx("@lilith 你看 @yuki 怎么样")).role == "lilith"
    # lilith 不在场但 yuki 在场：取第一个在场者 yuki（不返回 None）
    ctx = _ctx("@lilith 你看 @yuki 怎么样")
    ctx.present_roles = {"yuki"}
    assert r.evaluate(ctx).role == "yuki"
    # 全部命中角色不在场：mention-not-present
    ctx2 = _ctx("@lilith 你看 @yuki 怎么样")
    ctx2.present_roles = set()
    v = r.evaluate(ctx2)
    assert v.role is None
    assert v.reason == "mention-not-present"


def test_intent_rule_routes_to_lead():
    r = IntentRule()
    assert r.evaluate(_ctx("下播", lead="yuki")).role == "yuki"
    assert r.evaluate(_ctx("!状态", lead="lilith")).role == "lilith"
    # 反例：无意图文本不应命中
    v = r.evaluate(_ctx("今天天气不错"))
    assert v.role is None
    assert v.reason == "no-intent"


def test_relevance_rule():
    r = RelevanceRule()
    assert r.evaluate(_ctx("Yuki讲个笑话")).role is None  # 无关键词命中
    assert r.evaluate(_ctx("讲个故事吧")).role == "yuki"


def test_relevance_rule_bad_regex_no_crash():
    # 非法正则 "regex:[" 不应抛异常，空串 pattern 应被跳过，均计 0 分
    r = RelevanceRule()
    v = r.evaluate(_ctx_raw("随便聊聊", profiles=BadPatternProfiles()))
    assert v.role is None
    assert v.reason == "no-keyword-hit"


def test_cooldown_rule_prefers_idle():
    r = CooldownRule()
    assert r.evaluate(_ctx("随便聊聊")).role == "yuki"  # yuki 闲置更久


def test_cooldown_rule_tie_falls_back():
    # 两角色闲置时间相同 -> 平局交回链尾 RandomRule，不依赖 set 迭代序
    r = CooldownRule()
    v = r.evaluate(_ctx_raw("随便聊聊", tracker=FakeTT({"yuki": 100.0, "lilith": 100.0})))
    assert v.role is None
    assert v.reason == "tie"
    assert v.confidence == 0.6


def test_random_rule_never_none():
    r = RandomRule(seed=1)
    verdict = r.evaluate(_ctx("随便聊聊"))
    assert verdict.role in {"yuki", "lilith"}


def test_random_rule_last_choice_alternates():
    # last_choice 记录后，连续两次调用应偏向另一角色（交替）
    r = RandomRule(seed=1)
    first = r.evaluate(_ctx("随便聊聊")).role
    second = r.evaluate(_ctx("随便聊聊")).role
    assert first in {"yuki", "lilith"}
    assert second in {"yuki", "lilith"}
    assert first != second


def test_make_rules_by_order_seed_and_unknown():
    # seed 透传：同 seed 构造的 random 规则首次结果一致（重复调用不重新随机）
    rules = make_rules_by_order(["random", "no-such-rule"], seed=7)
    assert [r.name for r in rules] == ["random"]
    assert rules[0].evaluate(_ctx("随便聊聊")).role == \
        RandomRule(seed=7).evaluate(_ctx("随便聊聊")).role


def test_continuation_rule_follows_last_speaker():
    tt = FakeTTWithHistory(history=[{"role": "yuki", "kind": "speech",
                                     "text": "今天给大家讲一个月亮邮差的故事", "ts": 1.0}])
    ctx = ArbitrationContext(text="这个故事真好听，再来一个", user_name="观众", source="danmaku",
                             kind="danmaku", lead_role="yuki",
                             present_roles={"yuki", "lilith"},
                             profiles=FakeProfiles(), turn_tracker=tt)
    v = ContinuationRule().evaluate(ctx)
    assert v.role == "yuki" and v.reason == "continuation:yuki"


def test_continuation_rule_no_signal_returns_none():
    tt = FakeTTWithHistory(history=[{"role": "lilith", "kind": "speech",
                                     "text": "哼，今天直播人气不错", "ts": 1.0}])
    ctx = ArbitrationContext(text="今天天气不错", user_name="观众", source="danmaku",
                             kind="danmaku", lead_role="yuki",
                             present_roles={"yuki", "lilith"},
                             profiles=FakeProfiles(), turn_tracker=tt)
    v = ContinuationRule().evaluate(ctx)
    assert v.role is None


def test_continuation_rule_empty_history_safe():
    ctx = ArbitrationContext(text="嗯嗯", user_name="观众", source="danmaku",
                             kind="danmaku", lead_role="yuki",
                             present_roles={"yuki", "lilith"},
                             profiles=FakeProfiles(), turn_tracker=FakeTTWithHistory())
    v = ContinuationRule().evaluate(ctx)
    assert v.role is None


def test_balance_rule_prefers_other_after_monopoly():
    tt = FakeTTWithHistory(history=[
        {"role": "yuki", "kind": "speech", "text": "故事一", "ts": 1.0},
        {"role": "yuki", "kind": "speech", "text": "故事二", "ts": 2.0},
    ])
    ctx = ArbitrationContext(text="随便聊聊", user_name="观众", source="danmaku",
                             kind="danmaku", lead_role="yuki",
                             present_roles={"yuki", "lilith"},
                             profiles=FakeProfiles(), turn_tracker=tt)
    v = BalanceRule(max_run=2).evaluate(ctx)
    assert v.role == "lilith" and v.reason == "balance:lilith"


def test_balance_rule_not_fire_below_run_threshold():
    tt = FakeTTWithHistory(history=[{"role": "yuki", "kind": "speech", "text": "故事一", "ts": 1.0}])
    ctx = ArbitrationContext(text="随便聊聊", user_name="观众", source="danmaku",
                             kind="danmaku", lead_role="yuki",
                             present_roles={"yuki", "lilith"},
                             profiles=FakeProfiles(), turn_tracker=tt)
    assert BalanceRule(max_run=2).evaluate(ctx).role is None
