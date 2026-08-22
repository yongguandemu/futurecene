"""test_api_config.py — /api/config 路由单测（任务五 5.2）"""
import tempfile
from pathlib import Path

from src.shared.user_config import UserConfigStore
from src.web.app_factory import create_app


def _make_app(tmp_path, switch_manager=None):
    store = UserConfigStore(data_file=str(tmp_path / "cu.json"))
    context = {"user_config": store, "switch_manager": switch_manager}
    return create_app(context), store


def test_get_config_defaults(tmp_path):
    app, store = _make_app(tmp_path)
    r = app.test_client().get("/api/config")
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["settings"]["memory_strength"] == "medium"
    assert data["settings"]["allow_memory_to_worldbook"] is False
    assert set(data["options"]["memory_strength"]) == {"low", "medium", "high", "ultra"}
    assert data["reasoning_map"]["enhanced"]["engine"] == "pro"


def test_put_config_valid(tmp_path):
    app, store = _make_app(tmp_path)
    r = app.test_client().put("/api/config", json={
        "memory_strength": "high", "tts_output_target": "both"})
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["settings"]["memory_strength"] == "high"
    assert data["settings"]["tts_output_target"] == "both"
    assert data["applied"]["memory_strength"] == "high"


def test_put_config_invalid_value(tmp_path):
    app, store = _make_app(tmp_path)
    r = app.test_client().put("/api/config", json={"memory_strength": "bogus"})
    assert r.status_code == 400
    assert "取值非法" in r.get_json()["error"]


def test_put_config_no_valid_fields(tmp_path):
    app, store = _make_app(tmp_path)
    r = app.test_client().put("/api/config", json={"unknown": 1})
    assert r.status_code == 400
    assert "无有效设置字段" in r.get_json()["error"]


def test_put_config_worldbook_switch_side_effect(tmp_path):
    """allow_memory_to_worldbook PUT 即时同步 switch_manager。"""
    from src.shared.event_bus import EventBus
    from src.commander.switch_manager import SwitchManager

    bus = EventBus()
    bus.reset()
    switch_manager = SwitchManager(bus)
    switch_manager.auto_register("allow_memory_to_worldbook", default=False)
    app, store = _make_app(tmp_path, switch_manager=switch_manager)
    r = app.test_client().put("/api/config",
                              json={"allow_memory_to_worldbook": True})
    assert r.status_code == 200
    assert switch_manager.is_enabled("allow_memory_to_worldbook") is True
    # 持久化恢复：重启后装配层依 user_config 恢复
    store2 = UserConfigStore(data_file=str(tmp_path / "cu.json"))
    assert store2.get("allow_memory_to_worldbook") is True


def test_config_requires_user_config():
    app = create_app({})
    r = app.test_client().get("/api/config")
    assert r.status_code == 503
