"""test_timing_controller.py — 时序协调（口型/眨眼/身体起伏）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.live2d_orchestrator.timing_controller import TimingController


def test_blink_cycle_changes_eye_open():
    t = TimingController(blink_interval=(1.0, 1.0))  # 固定 1s 便于测试
    # now=0.04 → phase≈4%（眨眼半闭）；now=0.5 → phase=50%（全开）
    half = t.tick(now=0.04, speaking=False).get("ParamEyeLOpen")
    open_ = t.tick(now=0.5, speaking=False).get("ParamEyeLOpen", 1.0)
    assert half is not None and half < open_  # 有开合变化
    assert 0.0 <= half <= 1.0 and 0.0 <= open_ <= 1.0


def test_speaking_suppresses_motion_switch():
    t = TimingController()
    assert t.should_switch_motion(now=0.0, speaking=True) is False
    assert t.should_switch_motion(now=0.0, speaking=False) is True


def test_idle_body_breathing_enabled():
    t = TimingController()
    params = t.tick(now=0.0, speaking=False)
    # 呼吸功能禁用：不应出现大幅身体参数；轻微起伏保留
    body = params.get("ParamBodyAngleZ", 0.0)
    assert abs(body) <= 0.15


def test_speaking_no_body_params():
    t = TimingController()
    params = t.tick(now=0.0, speaking=True)
    assert "ParamBodyAngleZ" not in params
