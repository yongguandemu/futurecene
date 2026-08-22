"""test_parameter_registry.py — Live2D 参数注册表"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.live2d_orchestrator.parameter_registry import ParameterRegistry


def _write_model(tmp_path, name="Haru"):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.model3.json").write_text(json.dumps({
        "Version": 3, "FileReferences": {},
        "Parameters": [
            {"Id": "ParamAngleX", "Type": "Float", "Min": -30.0, "Max": 30.0, "Default": 0.0},
            {"Id": "ParamEyeLOpen", "Type": "Float", "Min": 0.0, "Max": 1.0, "Default": 1.0},
            {"Id": "ParamMouthOpenY", "Type": "Float", "Min": 0.0, "Max": 1.0, "Default": 0.0},
        ]}, ensure_ascii=False))
    return d


def test_load_parses_parameters(tmp_path):
    d = _write_model(tmp_path)
    reg = ParameterRegistry(models_dir=str(d))
    params = reg.load("Haru")
    assert "ParamAngleX" in params
    assert params["ParamAngleX"] == {"min": -30.0, "max": 30.0, "default": 0.0}
    assert params["ParamMouthOpenY"]["max"] == 1.0


def test_load_cached(tmp_path):
    d = _write_model(tmp_path)
    reg = ParameterRegistry(models_dir=str(d))
    assert reg.load("Haru") is reg.load("Haru")  # 缓存同一实例


def test_missing_model_returns_empty(tmp_path):
    reg = ParameterRegistry(models_dir=str(tmp_path))
    assert reg.load("NoSuchModel") == {}


def test_get_returns_none_for_unknown(tmp_path):
    d = _write_model(tmp_path)
    reg = ParameterRegistry(models_dir=str(d))
    reg.load("Haru")
    assert reg.get("Haru", "ParamNope") is None
