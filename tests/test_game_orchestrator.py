"""test_game_orchestrator.py — 游戏实况调度官单测（mock 屏幕/会话，不真实轮询）"""
import asyncio

from src.orchestrators.game_orchestrator import registry
from src.orchestrators.game_orchestrator.game_orchestrator import GameOrchestrator
from src.orchestrators.game_orchestrator.mc_bridge import MCBridge
from src.orchestrators.game_orchestrator.vn_session import VNProfile, VNSession
from src.shared.event_bus import EventBus
from src.shared.events import GAME_COMMENTARY_REQUESTED, GAME_VN_STATE_CHANGED


class FakeSession(VNSession):
    def __init__(self, state="对白"):
        self.state = state
        self._running = False
        self.started = False
        self.stopped = False
        self.profile = VNProfile(name="atri", window_title="ATRI")

    def start(self):
        self.started = True
        self._running = True

    def stop(self):
        self.stopped = True
        self._running = False

    def snapshot(self):
        return {"profile": "atri", "state": self.state, "running": self._running}


def _make(session=None):
    bus = EventBus()
    bus.reset()
    orch = GameOrchestrator(event_bus=bus, session=session)
    orch.start()
    return orch, bus


def test_capabilities_from_registry():
    orch, _ = _make()
    assert orch.capabilities() == registry.capabilities() == [
        "game:vn_start", "game:vn_stop", "game:vn_state",
        "game:mc_start", "game:mc_stop", "game:commentary",
    ]


def test_vn_start_uses_injected_session():
    session = FakeSession()
    orch, _ = _make(session=session)
    r = asyncio.run(orch.handle({"capability": "game:vn_start",
                                 "payload": {"profile_name": "atri"}}))
    assert r["ok"] is True and session.started is True


def test_vn_stop_and_state():
    session = FakeSession(state="菜单")
    orch, _ = _make(session=session)
    r = asyncio.run(orch.handle({"capability": "game:vn_state", "payload": {}}))
    assert r["data"]["state"] == "菜单"
    r2 = asyncio.run(orch.handle({"capability": "game:vn_stop", "payload": {}}))
    assert r2["ok"] is True and session.stopped is True


def test_vn_state_before_start():
    orch, _ = _make(session=None)
    r = asyncio.run(orch.handle({"capability": "game:vn_state", "payload": {}}))
    assert r["data"]["state"] == "not_started"


def test_mc_start_when_bot_missing():
    orch, _ = _make()
    orch._bridge = MCBridge(bot_path="nonexistent/bot.js")
    r = asyncio.run(orch.handle({"capability": "game:mc_start", "payload": {}}))
    assert r["ok"] is False  # bot.js 未迁移 → started=False


def test_commentary_publishes_request():
    orch, bus = _make()
    seen = {}
    bus.subscribe(GAME_COMMENTARY_REQUESTED, lambda event, **kw: seen.update(kw))
    r = asyncio.run(orch.handle({"capability": "game:commentary",
                                 "payload": {"scene_state": "对白"}}))
    assert r["ok"] is True
    seen.pop("seq", None)  # seq 为事件元数据，不属于业务载荷
    assert seen == {"scene_state": "对白"}


def test_unknown_capability():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "game:unknown", "payload": {}}))
    assert r["ok"] is False


def test_health():
    orch, _ = _make(session=FakeSession())
    assert orch.health()["status"] == "ok"
