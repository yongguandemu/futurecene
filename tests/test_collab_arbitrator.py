"""仲裁器单测：规则链 + 互斥 + 排队 + deferred 标记 + seed 透传。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.shared.event_bus import EventBus
from src.orchestrators.collaboration.arbitrator import SpeakerArbitrator
from src.orchestrators.collaboration.turn_tracker import TurnTracker


class FakeProfiles:
    def keywords_for(self, role):
        return {"yuki": {"topics": ["故事"], "patterns": []},
                "lilith": {"topics": ["直播"], "patterns": []}}[role]


class EmptyProfiles:
    """无在场角色：全链无命中场景（用于空 present_roles 不崩溃 + deferred=False）。"""

    def all_roles(self):
        return []

    def keywords_for(self, role):
        return {}


def _make():
    bus = EventBus()          # EventBus 为单例：reset 隔离测试间订阅与历史
    bus.reset()
    tt = TurnTracker()
    arb = SpeakerArbitrator(bus, tt, profiles=FakeProfiles(), lead_role="yuki")
    return bus, tt, arb


def test_arbitrate_mention():
    bus, tt, arb = _make()
    verdict = arb.arbitrate("danmaku", "@Lilith 你说呢", "观众", kind="danmaku")
    assert verdict.role == "lilith"
    assert verdict.rule_hit == "mention:lilith"
    assert verdict.deferred is False            # acquire 成功放行


def test_arbitrate_queues_while_speaking():
    bus, tt, arb = _make()
    got = []
    bus.subscribe("speech:arbitrated", lambda **kw: got.append(kw))
    tt.acquire("yuki")                          # 模拟 yuki 正在发言
    verdict = arb.arbitrate("danmaku", "@Lilith 你说呢", "观众", kind="danmaku")
    assert verdict.role is None                 # 未放行（互斥）
    assert verdict.deferred is True             # 排队待发
    assert tt.pending_count() == 1
    assert got and got[0]["deferred"] is True   # 入队分支事件携带 deferred=True
    tt.release("yuki")
    popped = tt.dequeue()
    assert popped["role"] == "lilith"


def test_arbitrate_publishes_event():
    bus, tt, arb = _make()
    got = []
    bus.subscribe("speech:arbitrated", lambda **kw: got.append(kw))
    arb.arbitrate("danmaku", "随便聊聊", "观众", kind="danmaku")
    assert got and got[0]["role"] in {"yuki", "lilith"}
    assert got[0]["rule_hit"]
    assert got[0]["deferred"] is False          # 放行分支事件携带 deferred=False


def test_empty_present_roles_no_crash():
    bus = EventBus()
    bus.reset()
    tt = TurnTracker()
    arb = SpeakerArbitrator(bus, tt, profiles=EmptyProfiles(), lead_role="yuki")
    verdict = arb.arbitrate("danmaku", "随便聊聊", "观众", kind="danmaku")
    assert verdict.role is None                 # 全链无命中，不崩溃
    assert verdict.deferred is False            # 无人回应 ≠ 排队待发


def test_priority_mapping():
    prio = SpeakerArbitrator._priority
    assert prio("danmaku", "mention:lilith") == 0
    assert prio("danmaku", "intent:状态") == 1
    assert prio("danmaku", "command:!开播") == 1
    assert prio("danmaku", "relevance:yuki") == 2
    assert prio("collab", "cooldown:yuki") == 3
    assert prio("active", "random:yuki") == 4
    assert prio("wechat", "no-hit") == 5


def test_rules_order_seed_passthrough():
    """rules_order 给定且 seed 相同 → RandomRule 行为一致（seed 已透传）。"""
    bus = EventBus()
    bus.reset()

    def make(seed):
        return SpeakerArbitrator(bus, TurnTracker(), profiles=FakeProfiles(),
                                 lead_role="yuki", rules_order=["random"],
                                 seed=seed)

    a, b = make(42), make(42)
    va = a.arbitrate("danmaku", "随便聊聊", "观众", kind="danmaku")
    vb = b.arbitrate("danmaku", "随便聊聊", "观众", kind="danmaku")
    assert va.role == vb.role
    assert va.role in {"yuki", "lilith"}
