"""test_user_config.py — 用户设置存储单测（任务五 5.2）"""
import pytest

from src.shared.user_config import DEFAULTS, UserConfigStore


def _make(tmp_path):
    return UserConfigStore(data_file=str(tmp_path / "config_user.json"))


def test_defaults_when_empty(tmp_path):
    store = _make(tmp_path)
    assert store.all() == DEFAULTS
    assert store.get("memory_strength") == "medium"
    assert store.get("allow_memory_to_worldbook") is False


def test_set_and_get(tmp_path):
    store = _make(tmp_path)
    assert store.set("memory_strength", "high") is True
    assert store.get("memory_strength") == "high"
    assert store.set("allow_memory_to_worldbook", True) is True
    assert store.get("allow_memory_to_worldbook") is True


def test_invalid_value_rejected(tmp_path):
    store = _make(tmp_path)
    with pytest.raises(ValueError):
        store.set("memory_strength", "bogus")
    with pytest.raises(ValueError):
        store.set("tts_output_target", "mars")
    with pytest.raises(ValueError):
        store.set("reasoning_intensity", "turbo")
    with pytest.raises(ValueError):
        store.set("unknown_key", 1)


def test_persistence_across_reload(tmp_path):
    path = tmp_path / "config_user.json"
    store = UserConfigStore(data_file=str(path))
    store.set("memory_strength", "ultra")
    store.set("tts_output_target", "both")
    store2 = UserConfigStore(data_file=str(path))
    assert store2.get("memory_strength") == "ultra"
    assert store2.get("tts_output_target") == "both"
    # 未设置项回落默认
    assert store2.get("reasoning_intensity") == "standard"


def test_case_insensitive_values(tmp_path):
    store = _make(tmp_path)
    store.set("memory_strength", "HIGH")
    assert store.get("memory_strength") == "high"
