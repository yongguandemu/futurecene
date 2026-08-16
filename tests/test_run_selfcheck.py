"""test_run_selfcheck.py — 启动自检（端口占用 / TTS 依赖提示）

覆盖：_port_in_use 对空闲/占用端口的判断、_ffmpeg_available 检测、
_check_tts_deps 在 ffmpeg 缺失时打印提示（不抛错）。
"""
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run as run_mod


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_port_free_detected_as_free():
    """空闲端口 → _port_in_use 返回 False。"""
    port = _free_port()
    assert run_mod._port_in_use(port) is False


def test_port_in_use_detected():
    """监听中的端口 → _port_in_use 返回 True（防止双进程）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert run_mod._port_in_use(port) is True
    finally:
        s.close()


def test_ffmpeg_available_detects_installed():
    """ffmpeg 可用性检测：不抛异常（结果依环境而定，仅验证可调用）。"""
    assert isinstance(run_mod._ffmpeg_available(), bool)


def test_check_tts_deps_no_crash_when_missing(monkeypatch, capsys):
    """imageio_ffmpeg 缺失 → 打印警告，不抛异常。"""
    monkeypatch.setattr(run_mod, "_ffmpeg_available", lambda: False)
    run_mod._check_tts_deps()  # 不抛异常
    out = capsys.readouterr().out
    assert "imageio_ffmpeg" in out and "警告" in out


def test_check_tts_deps_silent_when_present(monkeypatch, capsys):
    """imageio_ffmpeg 可用 → 无提示输出。"""
    monkeypatch.setattr(run_mod, "_ffmpeg_available", lambda: True)
    run_mod._check_tts_deps()  # 不抛异常
    out = capsys.readouterr().out
    assert out.strip() == ""
