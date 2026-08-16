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
        "game:op_start", "game:op_stop", "game:op_state",
        "game:op_plan", "game:op_command",
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


# ---------- 通用游戏操作（op_*） ----------

def test_op_start_enables_controller():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "game:op_start",
                                 "payload": {"window_title": "ATRI"}}))
    assert r["ok"] is True
    assert orch._op_controller.enabled is True
    assert orch._op_loop.snapshot()["running"] is True


def test_op_stop_disables_controller():
    orch, _ = _make()
    asyncio.run(orch.handle({"capability": "game:op_start", "payload": {}}))
    r = asyncio.run(orch.handle({"capability": "game:op_stop", "payload": {}}))
    assert r["ok"] is True
    assert orch._op_controller.enabled is False
    assert orch._op_loop.snapshot()["running"] is False


def test_op_state_snapshot():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "game:op_state", "payload": {}}))
    assert r["ok"] is True
    assert "controller" in r["data"]
    assert "safety" in r["data"]


def test_op_plan_template():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "game:op_plan",
                                 "payload": {"command": "跳跃"}}))
    assert r["ok"] is True
    assert r["data"]["plan"] == [{"action": "keypress", "params": {"key": "SPACE"}}]


def test_op_plan_missing_command():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "game:op_plan", "payload": {}}))
    assert r["ok"] is False


def test_op_command_requires_start():
    orch, _ = _make()
    r = asyncio.run(orch.handle({"capability": "game:op_command",
                                 "payload": {"action": "advance"}}))
    assert r["ok"] is False  # 未开启自动操作


def test_op_command_structured_action():
    orch, _ = _make()
    asyncio.run(orch.handle({"capability": "game:op_start", "payload": {}}))
    orch._op_act = lambda action, params: {"ok": True, "scene_changed": False}
    r = asyncio.run(orch.handle({"capability": "game:op_command",
                                 "payload": {"action": "advance"}}))
    assert r["ok"] is True


def test_op_command_natural_language():
    orch, _ = _make()
    asyncio.run(orch.handle({"capability": "game:op_start", "payload": {}}))
    orch._op_act = lambda action, params: {"ok": True, "scene_changed": False}
    r = asyncio.run(orch.handle({"capability": "game:op_command",
                                 "payload": {"command": "跳跃"}}))
    assert r["ok"] is True


# ---------- 联动：LLM 规划 / 经验学习 / 解说 ----------

class FakeLLMOrch:
    name = "llm"

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def handle(self, command):
        self.calls.append(command)
        return {"ok": True, "data": {"reply": self.reply}, "error": None}


class FakeExpOrch:
    name = "experience"

    def __init__(self):
        self.calls = []

    async def handle(self, command):
        self.calls.append(command)
        return {"ok": True, "data": {"feedbacked": True}, "error": None}


def test_op_chat_routes_to_llm_orchestrator():
    """LLM 规划联动：注入 llm 调度官后 _op_chat 经 llm:chat 取回复（pro 引擎，DeepSeek 优先）。"""
    llm = FakeLLMOrch('[{"action": "keypress", "params": {"key": "E"}}]')
    orch, _ = _make()
    orch.set_llm_orchestrator(llm)
    plan = orch._op_planner.generate_plan("打开地图")  # 不在模板表，走 LLM 路径
    assert plan == [{"action": "keypress", "params": {"key": "E"}}]
    assert llm.calls and llm.calls[0]["capability"] == "llm:chat"
    assert llm.calls[0]["payload"].get("engine") == "pro"  # 复杂 Agent 任务走 DeepSeek V4 Pro


def test_op_chat_without_llm_returns_empty():
    """未注入 llm 调度官时 _op_chat 返回空（走模板路径）。"""
    orch, _ = _make()
    assert orch._op_chat("随便") == ""


def test_op_experience_routes_to_experience_orchestrator():
    """经验学习联动：操作成功且场景变化 → experience:feedback。"""
    exp = FakeExpOrch()
    orch, _ = _make()
    orch.set_experience_orchestrator(exp)
    orch._op_experience("advance", {}, {"text": "对白"})
    assert exp.calls and exp.calls[0]["capability"] == "experience:feedback"
    assert exp.calls[0]["payload"]["event_positive"] is True


def test_op_commentary_publishes_request():
    """解说联动：操作后发布 game:commentary_requested。"""
    orch, bus = _make()
    seen = {}
    bus.subscribe(GAME_COMMENTARY_REQUESTED, lambda event, **kw: seen.update(kw))
    orch._op_commentary("advance", {"text": "你好世界"})
    seen.pop("seq", None)
    assert seen == {"scene_state": "你好世界"}


class FakeScreen:
    name = "screen"

    def __init__(self):
        self.calls = []

    async def handle(self, command):
        self.calls.append(command)
        return {"ok": True, "data": {"done": True}, "error": None}


def test_op_act_success_no_retry_duplication():
    """操作执行成功即返回 scene_changed=True，避免重试机制重复执行操作。"""
    screen = FakeScreen()
    orch, _ = _make()
    orch._screen = screen
    r = orch._op_act("advance", {})
    assert r["ok"] is True and r["scene_changed"] is True
    assert screen.calls == [{"capability": "screen:click",
                             "payload": {"window_title": "", "backend": "auto",
                                         "x": 960, "y": 940, "label": "推进"}}]
