"""test_web_routes.py — 网关端点（/api/health 快照、/api/state、/api/command、/api/switch）"""
import asyncio

from src.commander.command_router import CommandRouter
from src.commander.intent_parser import IntentParser
from src.commander.orchestrator_registry import OrchestratorRegistry
from src.commander.session_context import SessionContext
from src.commander.switch_manager import SwitchManager
from src.shared.event_bus import EventBus
from src.web.app_factory import create_app


class FakeOrchestrator:
    def __init__(self, name, capabilities):
        self.name = name
        self._capabilities = capabilities

    def capabilities(self):
        return list(self._capabilities)

    async def handle(self, command):
        return {"ok": True, "data": {"reply": f"{self.name}: done"}, "error": None}

    def start(self):
        pass

    def stop(self):
        pass

    def health(self):
        return {"status": "ok", "detail": ""}


def _make_context():
    bus = EventBus()
    bus.reset()
    sm = SwitchManager(bus)
    reg = OrchestratorRegistry(sm, bus)
    reg.register(FakeOrchestrator("llm", ["llm:chat"]))
    session = SessionContext(session_id="test")
    session.bind_event_bus(bus)
    router = CommandRouter(reg, sm, bus)
    return {"event_bus": bus, "switch_manager": sm, "registry": reg,
            "session": session, "intent_parser": IntentParser(),
            "command_router": router}


def test_health_with_registry_snapshot():
    app = create_app(_make_context())
    client = app.test_client()
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["orchestrators"] == ["llm"]  # 注册表快照（M3 验收）


def test_health_without_context_stays_minimal():
    app = create_app()
    resp = app.test_client().get("/api/health")
    assert resp.get_json() == {"status": "ok"}  # M0 契约不破坏


def test_state_snapshot():
    app = create_app(_make_context())
    client = app.test_client()
    resp = client.get("/api/state")
    data = resp.get_json()
    assert data["session"]["role"] == "yuki"
    assert data["switches"] == {"llm": True}
    assert data["orchestrators"] == ["llm"]


def test_command_routes_to_orchestrator():
    app = create_app(_make_context())
    client = app.test_client()
    resp = client.post("/api/command", json={"text": "你好"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["data"]["reply"] == "llm: done"


def test_command_switch_role_internal():
    app = create_app(_make_context())
    client = app.test_client()
    resp = client.post("/api/command", json={"text": "!切换 lilith"})
    data = resp.get_json()
    assert data["ok"] is True
    assert data["data"]["role"] == "lilith"


def test_command_empty_text_400():
    app = create_app(_make_context())
    resp = app.test_client().post("/api/command", json={"text": "  "})
    assert resp.status_code == 400


def test_switch_control():
    app = create_app(_make_context())
    client = app.test_client()
    resp = client.post("/api/switch/llm", json={"enabled": False})
    data = resp.get_json()
    assert data["ok"] is True
    assert data["data"]["enabled"] is False
    # 开关禁用后路由被拦截
    resp2 = client.post("/api/command", json={"text": "你好"})
    assert resp2.get_json()["ok"] is False


def test_switch_missing_enabled_400():
    app = create_app(_make_context())
    resp = app.test_client().post("/api/switch/llm", json={})
    assert resp.status_code == 400


def test_state_returns_version():
    # 用既有 test app fixture 写法（无 build_test_app helper，适配现有 _make_context）
    app = create_app(_make_context())
    client = app.test_client()
    resp = client.get("/api/state")
    data = resp.get_json()
    assert "version" in data
    assert isinstance(data["version"], int)


def test_assistant_redirects_to_dashboard():
    app = create_app(_make_context())
    client = app.test_client()
    resp = client.get("/assistant/")
    assert resp.status_code == 302
    assert "/dashboard/#assistant" in resp.headers["Location"]
