"""test_p2_app_boot.py — P2 接线回归：完整装配后应注册 13 个调度官

跳过真实 .env 依赖，用 monkeypatch 注入全部必填环境变量（规格书 6.2）。
config/config.yaml 的 P2 配置段（music/platform/stream/experience）会被加载。
P1 直播间智能调度官（intelligence）亦随装配注册。
"""
import pytest

from src.commander.orchestrator_registry import OrchestratorRegistry
from src.shared.config_loader import ConfigLoader

REQUIRED_ENV = {
    "OPENAI_API_KEY": "sk-test",
    "OPENAI_BASE_URL": "https://openai.example.com/v1",
    "OPENAI_MODEL": "gpt-5.5",
    "ZHIPU_API_KEY": "zhipu-test",
    "ZHIPU_MODEL": "glm-5.2",
    "DASHSCOPE_API_KEY": "dash-test",
    "WUSOUND_API_KEY": "wusound-test",
    "BILIBILI_ACCESS_KEY_ID": "bili-id",
    "BILIBILI_ACCESS_KEY_SECRET": "bili-secret",
    "BILIBILI_COOKIE": "bili-cookie",
    "OBS_WS_PASSWORD": "obs-test",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)


def test_all_p2_orchestrators_wire(monkeypatch):
    from src.app import build_app_context

    app, event_bus = build_app_context()
    ctx = app.config["APP_CONTEXT"]
    registry: OrchestratorRegistry = ctx["registry"]
    names = {o.name for o in registry.all()}
    expected = {"llm", "tts", "live2d", "bilibili", "memory", "safety",
                "screen", "game", "music", "platform", "stream", "experience",
                "intelligence"}
    assert names == expected  # 13 个，不多不少


def test_p2_config_sections_present(monkeypatch):
    loader = ConfigLoader()
    assert loader.get("music") is not None
    assert "default_volume" in loader.get("music")
    assert loader.get("platform") is not None
    assert loader.get("stream") is not None
    assert loader.get("experience") is not None
    # 复用 OBS 密码占位符已解析为非空
    assert loader.get("platform.obs.password") == "obs-test"


def test_p2_capabilities_routable(monkeypatch):
    from src.app import build_app_context

    app, _ = build_app_context()
    ctx = app.config["APP_CONTEXT"]
    registry: OrchestratorRegistry = ctx["registry"]
    # 每个 P2 能力都能匹配到对应调度官
    for cap in ("music:play", "adapter:connect", "stream:start",
                "experience:decide"):
        orch = registry.match(cap)
        assert orch is not None, f"capability {cap} 未路由"
        assert cap in orch.capabilities()