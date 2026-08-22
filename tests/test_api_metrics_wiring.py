"""test_api_metrics_wiring.py — 用量监控接线集成测试（任务五 5.3）

验证装配层成本记录接线：LLM_RESPONDED（带 usage/model）→ CostTracker.record("llm")，
TTS_REQUESTED（带 text）→ record("tts", chars)；/api/metrics 的 cost.today 反映真实调用。
"""
import pytest

from src.shared.event_bus import EventBus
from src.shared.events import LLM_RESPONDED, TTS_REQUESTED

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


def _boot():
    EventBus().reset()
    from src.app import build_app_context
    app, event_bus = build_app_context()
    return app, event_bus


def test_cost_recorded_from_events():
    """发布 LLM_RESPONDED / TTS_REQUESTED → 成本按日聚合（today 有数据）。"""
    app, event_bus = _boot()
    # 重置成本（隔离真实 data/cost.json 历史数据，只验证本次事件增量）
    ctx = app.config["APP_CONTEXT"]
    ctx["cost_tracker"].reset()
    event_bus.publish(LLM_RESPONDED, capability="llm:chat", text="你好",
                      usage={"prompt_tokens": 100, "completion_tokens": 50},
                      model="deepseek-v4-pro")
    event_bus.publish(TTS_REQUESTED, capability="tts:synthesize", text="今天天气不错")
    resp = app.test_client().get("/api/metrics")
    cost = resp.get_json()["cost"]
    assert cost["today"]["total_calls"] == 2
    assert cost["by_type"]["llm"]["calls"] == 1
    assert cost["by_type"]["tts"]["calls"] == 1
    assert cost["by_type"]["llm"]["tokens"] == 150


def test_metrics_config_and_memory_endpoints_available():
    """任务五新端点装配后全部可访问。"""
    app, _ = _boot()
    client = app.test_client()
    assert client.get("/api/config").status_code == 200
    assert client.get("/api/memory").status_code == 200
    assert client.get("/api/memory/recall").status_code == 200
    assert client.get("/api/memory/review").status_code == 200
    # 设置写入 → 重启恢复（allow_memory_to_worldbook 同步 switch）
    r = client.put("/api/config", json={"memory_strength": "ultra"})
    assert r.status_code == 200
    assert r.get_json()["data"]["settings"]["memory_strength"] == "ultra"
