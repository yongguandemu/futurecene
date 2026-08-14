"""test_config_loader.py — 必填环境变量校验 + ConfigLoader 占位符解析"""
import pytest

from src.shared.config_loader import (
    ConfigError,
    ConfigLoader,
    MANDATORY_ENV_VARS,
    get_missing_env_vars,
    validate_or_exit,
)


def test_all_missing_reported(monkeypatch):
    for var in MANDATORY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    missing = get_missing_env_vars()
    assert set(missing) == set(MANDATORY_ENV_VARS)


def test_empty_string_counts_as_missing(monkeypatch):
    for var in MANDATORY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "")  # 空字符串禁止继续运行（规格书 6.2 规则 2）
    missing = get_missing_env_vars()
    assert "OPENAI_API_KEY" in missing


def test_filled_vars_pass(monkeypatch):
    for var in MANDATORY_ENV_VARS:
        monkeypatch.setenv(var, "dummy_value")
    assert get_missing_env_vars() == []


def test_validate_or_exit_exits_on_missing(monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        validate_or_exit(["OPENAI_API_KEY"])
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "[config] missing required env: OPENAI_API_KEY" in out


def test_validate_or_exit_passes_when_ok(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy_value")
    validate_or_exit(["OPENAI_API_KEY"])  # 不应抛异常


# ---------- ConfigLoader 占位符解析（规格书 6.2 密钥管理规则） ----------

_SAMPLE_YAML = """
llm:
  openai:
    api_key: ${{ env.OPENAI_API_KEY }}
    model: gpt-4
bilibili:
  access_key_id: ${{ env.BILIBILI_ACCESS_KEY_ID }}
"""


def test_config_loader_resolves_placeholders(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-value")
    path = tmp_path / "config.yaml"
    path.write_text(_SAMPLE_YAML, encoding="utf-8")
    loader = ConfigLoader(config_path=str(path))
    assert loader.get("llm.openai.api_key") == "sk-real-value"
    assert loader.get("llm.openai.model") == "gpt-4"


def test_config_loader_missing_env_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(_SAMPLE_YAML, encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        ConfigLoader(config_path=str(path))
    assert "OPENAI_API_KEY" in str(exc.value)


def test_config_loader_deferred_env_passes(tmp_path, monkeypatch):
    """B站为延后必填：占位符引用缺失时放行为空，不抛错。"""
    monkeypatch.delenv("BILIBILI_ACCESS_KEY_ID", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    path = tmp_path / "config.yaml"
    path.write_text(_SAMPLE_YAML, encoding="utf-8")
    loader = ConfigLoader(config_path=str(path))
    assert loader.get("bilibili.access_key_id") == ""
    assert loader.get("llm.openai.api_key") == "x"


def test_config_loader_get_default():
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {"a": {"b": 1}}
    assert loader.get("a.b") == 1
    assert loader.get("a.missing", "fallback") == "fallback"


def test_config_loader_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        ConfigLoader(config_path=str(tmp_path / "nonexistent.yaml"))
