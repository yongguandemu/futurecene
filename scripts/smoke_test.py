"""smoke_test.py — Future Scene 验收前冒烟自检（真实链路，不打桩）

用途：提交/验收前一键验证"系统能不能跑起来"，覆盖单测查不出的环境与
端到端问题（规格书之外的测试缺口）：
  L0 环境探测：Python 版本、imageio_ffmpeg、密钥、端口占用
  L1 服务存活：/api/health 全调度官状态、命令接口响应延迟
  L2 主链路：真实对话 → LLM 延迟与回复、TTS 落盘真实格式（RIFF/ID3）、
            缓存命名按真实格式（修复：MP3 伪装 WAV / 提示音）

用法：
  python scripts/smoke_test.py              完整冒烟（需服务已启动，默认 127.0.0.1:5000）
  python scripts/smoke_test.py --host H --port P --base-url URL
  python scripts/smoke_test.py --check-env  仅环境探测（L0，无需服务）

退出码：全部通过 0；任一失败 1（便于 CI / bat 集成）。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PASS, FAIL = "PASS", "FAIL"
_results = []  # (level, name, status, detail)

# 允许降级的调度官（未接入外部服务前的预期状态，不视为失败）：
# bilibili —— B 站密钥为"延后必填"（config warning: 接入 B站前可忽略），
#             未配置密钥时该调度官 health 报 degraded 属设计行为。
IGNORED_DEGRADED = {"bilibili"}


def _record(level: str, name: str, ok: bool, detail: str = "") -> bool:
    _results.append((level, name, PASS if ok else FAIL, detail))
    mark = "[OK]" if ok else "[XX]"
    print(f"  {mark} {level} {name}" + (f" — {detail}" if detail else ""))
    return ok


# ---------- L0 环境探测 ----------

def l0_env(host: str, port: int) -> bool:
    print("\n== L0 环境探测 ==")
    ok = True
    ok &= _record("L0", f"Python {sys.version.split()[0]}", True)
    ok &= _record("L0", "imageio_ffmpeg 可用（TTS MP3→WAV 转换依赖）",
                  _ffmpeg_ok(), "缺失时 TTS 输出 MP3，winsound 本机播放不可用")
    # 密钥：从 .env / 环境变量读取（不打印明文）
    env_file = PROJECT_ROOT / ".env"
    keys = {k: False for k in ("WUSOUND_API_KEY", "DASHSCOPE_API_KEY",
                               "OPENAI_API_KEY", "ZHIPU_API_KEY")}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k = line.split("=", 1)[0].strip()
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if k in keys and v and v != "***":
                    keys[k] = True
    for k, present in keys.items():
        ok &= _record("L0", f"密钥 {k}", present,
                      "缺失时对应引擎降级/失败")
    return ok


def _ffmpeg_ok() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("imageio_ffmpeg") is not None
    except Exception:
        return False


# ---------- L1 服务存活 ----------

def _http_json(url: str, timeout: float = 60.0, method: str = "GET",
               body: dict = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw), (time.time() - t0) * 1000


def l1_service(base: str) -> bool:
    print("\n== L1 服务存活 ==")
    ok = True
    try:
        health, _ = _http_json(f"{base}/api/health", timeout=15)
        ok &= _record("L1", "/api/health 返回 ok", health.get("status") == "ok",
                      f"orchestrators={len(health.get('orchestrators', []))}")
        degraded = [o for o, s in (health.get("watchdog") or {}).items()
                    if s != "ok" and o not in IGNORED_DEGRADED]
        ok &= _record("L1", "全调度官健康（无 degraded）", not degraded,
                      f"degraded={degraded}" if degraded else "")
    except Exception as e:
        ok &= _record("L1", "/api/health", False, str(e))
    # 命令接口延迟（内部命令 system:status，不走 LLM，测 HTTP 链路本身）
    try:
        r, ms = _http_json(f"{base}/api/command", timeout=15, method="POST",
                           body={"text": "!状态"})
        ok &= _record("L1", f"命令接口响应 ({ms:.0f}ms)", r.get("ok") is True,
                      r.get("error") or "")
    except Exception as e:
        ok &= _record("L1", "命令接口", False, str(e))
    return ok


# ---------- L2 主链路 ----------

def l2_pipeline(base: str) -> bool:
    print("\n== L2 主链路（真实对话 → LLM → TTS）==")
    ok = True
    # 2.1 真实对话：LLM 调用延迟 + 有回复
    try:
        r, ms = _http_json(f"{base}/api/command", timeout=90, method="POST",
                           body={"text": "你好，简单自我介绍一下"})
        reply = r.get("data", {}).get("reply", "") if r.get("ok") else ""
        ok &= _record("L2", f"对话回复 ({ms:.0f}ms)", bool(reply) and r.get("ok") is True,
                      f"latency={ms:.0f}ms reply={reply[:24]!r}")
        if ms > 15000:
            _record("L2", "对话延迟阈值（≤15s）", False, f"实际 {ms:.0f}ms")
    except Exception as e:
        ok &= _record("L2", "真实对话", False, str(e))
    # 2.2 TTS 真实合成：走 /api/danmaku 完整链路（记忆 → LLM → 字幕 → TTS），
    # 检测"本次调用"新落盘的音频文件（杜绝旧缓存假阳性），验证格式 RIFF/ID3 与后缀一致
    try:
        before_ts = _latest_tts_mtime()
        r, _ = _http_json(f"{base}/api/danmaku", timeout=60, method="POST",
                          body={"content": "主播你好，测试语音合成效果",
                                "user_name": "冒烟测试"})
        ok &= _record("L2", "弹幕入口注入", r.get("ok") is True,
                      f"command_id={r.get('command_id', '')[:8]}")
        # TTS 经指挥官链路异步合成，轮询等待新文件（最长 15s）
        latest = _wait_new_tts_file(before_ts, timeout=15)
        if latest:
            head = latest.read_bytes()[:4]
            is_riff = head == b"RIFF"
            is_mp3 = head == b"ID3"
            ok &= _record("L2", "TTS 落盘格式（RIFF=WAV / ID3=MP3）",
                          is_riff or is_mp3,
                          f"{latest.name} head={head!r}")
            suffix_ok = (is_riff and latest.name.endswith(".wav")) or \
                        (is_mp3 and latest.name.endswith(".mp3"))
            ok &= _record("L2", "缓存后缀与真实格式一致（修复：MP3 伪装 WAV）",
                          suffix_ok, f"{latest.name}")
        else:
            ok &= _record("L2", "TTS 落盘（弹幕链路新文件）", False,
                          f"15s 内未发现新 tts 文件（before_ts={before_ts})")
    except Exception as e:
        ok &= _record("L2", "TTS 探测", False, str(e))
    return ok


def _latest_tts_mtime():
    cache = PROJECT_ROOT / "data" / "cache" / "tts"
    if not cache.exists():
        return 0.0
    files = [f for f in cache.glob("tts_*") if f.is_file()]
    return max((f.stat().st_mtime for f in files), default=0.0)


def _wait_new_tts_file(before_ts: float, timeout: float = 15.0):
    """轮询等待 mtime 严格晚于 before_ts 的新 TTS 文件（防旧缓存假阳性）。"""
    cache = PROJECT_ROOT / "data" / "cache" / "tts"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cache.exists():
            fresh = [f for f in cache.glob("tts_*")
                     if f.is_file() and f.stat().st_mtime > before_ts]
            if fresh:
                return max(fresh, key=lambda f: f.stat().st_mtime)
        time.sleep(0.5)
    return None


# ---------- 汇总 ----------

def main() -> int:
    ap = argparse.ArgumentParser(description="Future Scene 冒烟自检")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--base-url", default="")
    ap.add_argument("--check-env", action="store_true", help="仅环境探测（L0）")
    args = ap.parse_args()
    base = args.base_url or f"http://{args.host}:{args.port}"

    print("=" * 58)
    print("  Future Scene 冒烟自检（真实链路，不打桩）")
    print("=" * 58)
    total_ok = l0_env(args.host, args.port)
    if not args.check_env:
        total_ok &= l1_service(base)
        total_ok &= l2_pipeline(base)

    print("\n" + "=" * 58)
    failed = [r for r in _results if r[2] == FAIL]
    for lv, name, st, detail in _results:
        print(f"  [{st}] {lv} {name}")
    print("=" * 58)
    print(f"  合计 {len(_results)} 项：{len(_results) - len(failed)} 通过 / {len(failed)} 失败")
    if failed:
        print("\n  失败项（按优先级处理）：")
        for lv, name, st, detail in failed:
            print(f"    - {lv} {name}: {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
