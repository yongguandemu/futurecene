"""仲裁器单测：规则链 + 互斥 + 排队。"""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.shared.event_bus import EventBus
from src.orchestrators.collaboration.arbitrator import SpeakerArbitrator
from src.orchestrators.collaboration.turn_tracker import TurnTracker
from src.orchestrators.collaboration.rules import ArbitrationContext


class FakeProfiles:
    def keywords_for(self, role):
        return {"yuki": {"topics": ["故事"], "patterns": []},
                "lilith": {"topics": ["直播"], "patterns": []}}[role]


def _make():
    bus = EventBus()
    tt = TurnTracker()
    arb = SpeakerArbitrator(bus, tt, profiles=FakeProfiles(), lead_role="yuki")
    return bus, tt, arb


def test_arbitrate_mention():
    bus, tt, arb = _make()
    verdict = arb.arbitrate("danmaku", "@Lilith 你说呢", "观众", kind="danmaku")
    assert verdict.role == "lilith"
    assert verdict.rule_hit == "mention:lilith"


def test_arbitrate_queues_while_speaking():
    bus, tt, arb = _make()
    tt.acquire("yuki")                       # 模拟 yuki 正在发言
    verdict = arb.arbitrate("danmaku", "@Lilith 你说呢", "观众", kind="danmaku")
    assert verdict.role is None              # 未放行（互斥）
    assert tt.pending_count() == 1
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
