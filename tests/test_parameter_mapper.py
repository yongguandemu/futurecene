"""test_parameter_mapper.py — 情绪/动作 → Live2D 参数映射"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.live2d_orchestrator.parameter_mapper import ParameterMapper


class FakeRegistry:
    def get(self, model, pid):
        known = {"ParamSmile": {"min": -1.0, "max": 1.0},
                 "ParamEyeLOpen": {"min": 0.0, "max": 1.0},
                 "ParamAngleZ": {"min": -30.0, "max": 30.0},
                 "ParamMouthOpenY": {"min": 0.0, "max": 1.0}}
        return known.get(pid)


def _mapper():
    return ParameterMapper(registry=FakeRegistry())


def test_happy_maps_smile():
    m = _mapper()
    params = m.map("开心", model="Haru")
    assert "ParamSmile" in params
    assert 0.0 <= params["ParamSmile"] <= 1.0


def test_unknown_emotion_calm():
    m = _mapper()
    params = m.map("不存在", model="Haru")
    assert params == {}


def test_clamps_to_registry_range():
    m = _mapper()
    params = m.map("惊讶", model="Haru")
    assert "ParamEyeLOpen" in params
    assert 0.0 <= params["ParamEyeLOpen"] <= 1.0


def test_motion_merged():
    m = _mapper()
    params = m.map("平静", motion="wave", model="Haru")
    assert "ParamAngleZ" in params  # wave 动作参数
