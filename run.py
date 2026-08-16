"""run.py — Future Scene 一键启动入口（本地服务 + 控制台界面）

用法：
  python run.py               完整启动：环境校验 → 服务 → 自动打开浏览器
  python run.py --no-browser  不自动打开浏览器
  python run.py --host H      监听地址（默认 0.0.0.0）
  python run.py --port P      端口（默认 5000）

启动前自检（fail-fast，避免"测试全绿、验收一堆毛病"的环境差异）：
  - 端口占用：目标端口已被占用 → 报错退出（防止双进程互相干扰）
  - TTS 依赖：imageio_ffmpeg 缺失 → 提示主引擎会降级 MP3（可继续，WAV 播放受限）
  - 必填密钥：缺失硬退出（B站三项延后警告）

启动后：
  控制台界面  http://127.0.0.1:5000/dashboard/  （总控制台）
  世界书管理  http://127.0.0.1:5000/worldbook/
  Live2D 源  http://127.0.0.1:5000/live2d_stream/  （OBS 浏览器源）
  健康检查    http://127.0.0.1:5000/api/health
"""
import argparse
import logging
import socket
import sys
import threading
import time
import webbrowser

from src.shared.config_loader import get_missing_env_vars, load

logger = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5000


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """探测端口是否已被占用（连接测试；占用则说明已有实例在跑）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def _ffmpeg_available() -> bool:
    """检测 imageio_ffmpeg 是否可用（find_spec 不触发 import 副作用）。"""
    import importlib.util
    return importlib.util.find_spec("imageio_ffmpeg") is not None


def _check_tts_deps() -> None:
    """TTS 依赖自检：imageio_ffmpeg 缺失时主引擎（悟声）MP3→WAV 转换不可用，
    合成音频会以 MP3 落盘，本机播放（winsound）受限。提示而非阻断。"""
    if not _ffmpeg_available():
        print("[run] 警告：未安装 imageio_ffmpeg，TTS 主引擎（悟声）将输出 MP3，"
              "本机播放（winsound）不可用；"
              "建议 pip install imageio-ffmpeg（或改用 dashscope 兜底引擎）")


def _banner(host: str, port: int) -> None:
    line = "=" * 58
    print(line)
    print("  Future Scene · 智能虚拟角色自主直播系统")
    print(line)
    print("  控制台界面  http://127.0.0.1:{}/dashboard/".format(port))
    print("  世界书管理  http://127.0.0.1:{}/worldbook/".format(port))
    print("  Live2D 源   http://127.0.0.1:{}/live2d/  (OBS 浏览器源)".format(port))
    print("  字幕叠加    http://127.0.0.1:{}/subtitle/  (OBS 浏览器源)".format(port))
    print("  健康检查    http://127.0.0.1:{}/api/health".format(port))
    print(line)
    print("  停止服务：在窗口按 Ctrl+C")
    print(line)


def main() -> None:
    parser = argparse.ArgumentParser(description="Future Scene 一键启动")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--host", default=DEFAULT_HOST, help="监听地址（默认 %s）" % DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="端口（默认 %d）" % DEFAULT_PORT)
    args = parser.parse_args()

    # 0. 端口占用自检（fail-fast）：已有一个实例在跑 → 拒绝重复启动，避免双进程互相干扰
    if _port_in_use(args.port):
        print("[run] 错误：端口 {} 已被占用，检测到已有实例在运行。".format(args.port))
        print("[run] 如需重启请先停止旧进程，再运行本命令。")
        sys.exit(1)

    # 1. TTS 依赖自检（提示而非阻断）
    _check_tts_deps()

    # 2. 加载 .env 并校验必填环境变量（缺失硬退出，B站三项延后警告）
    load()
    missing = get_missing_env_vars()
    if missing:
        print("[run] 提示：缺失延后必填变量（不影响本地界面演示）: {}".format(", ".join(missing)))

    # 3. 装配全部调度官 + Web 服务
    from src.app import build_app_context
    app, _ = build_app_context()

    # 4. 自动打开浏览器（延迟到服务就绪）
    if not args.no_browser:
        url = "http://127.0.0.1:{}/dashboard/".format(args.port)
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    # 5. 启动
    _banner(args.host, args.port)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
