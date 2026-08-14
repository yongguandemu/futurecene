"""demo_danmaku.py — P1 核心链路本地演示

模拟一条弹幕驱动全链路（规格书 9.2）：
弹幕 → 输入安全 → 记忆检索 → LLM → 输出安全 → 字幕 → TTS → Live2D 口型。

用法：
    1. 配置 .env（真实密钥）
    2. python scripts/demo_danmaku.py [弹幕文本]
    3. 观察日志中的链路各环节（[DanmakuPipeline] 输出）
    4. 另开终端：python src/app.py 后可配合前端 /dashboard/ /subtitle/ 观察
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.app import build_app_context  # noqa: E402
from src.orchestrators.bilibili_orchestrator import normalizer  # noqa: E402
from src.shared.config_loader import load  # noqa: E402
from src.shared.events import (  # noqa: E402
    AUDIENCE_FILTERED,
    FRONTEND_SUBTITLE_UPDATE,
    LIVE2D_LIP_SYNC_START,
    TTS_AUDIO_READY,
)

DEMO_DANMAKU = "主播今天讲什么故事呀？"


def main() -> None:
    load()  # 加载 .env + 必填校验（与服务启动一致）
    app, event_bus = build_app_context()
    print("=" * 60)
    print("P1 核心链路演示（规格书 9.2）")
    print("=" * 60)

    # 预加载 Live2D 模型（口型同步前置条件，规格书 5.4）
    live2d = app.config["APP_CONTEXT"]["registry"].get("live2d")
    asyncio.run(live2d.handle({"capability": "live2d:load",
                               "payload": {"model_name": "小恶魔"}}))

    # 订阅链路关键事件（观察编排序列）
    events_log = []

    def _log(name):
        def handler(event, **kw):
            text = kw.get("text") or kw.get("content") or kw.get("audio_id") or ""
            events_log.append(f"  [{name}] {text}")
            print(f"  [{name}] {text}")
        return handler

    event_bus.subscribe(AUDIENCE_FILTERED, _log("输入安全拦截"))
    event_bus.subscribe(FRONTEND_SUBTITLE_UPDATE, _log("字幕"))
    event_bus.subscribe(TTS_AUDIO_READY, _log("TTS audio_ready"))
    event_bus.subscribe(LIVE2D_LIP_SYNC_START, _log("Live2D 口型"))

    text = sys.argv[1] if len(sys.argv) > 1 else DEMO_DANMAKU
    print(f"\n模拟弹幕: 「{text}」\n")

    # 模拟 B站归一化后发布弹幕事件（真实环境由 connector 的 WS 回调触发）
    danmaku = normalizer.normalize({"cmd": "DANMU_MSG",
                                    "data": {"text": text, "user_name": "观众甲",
                                             "user_id": 10086}})
    normalizer.publish(event_bus, danmaku)

    print("\n" + "=" * 60)
    if not events_log:
        print("链路无事件输出：请检查 .env 密钥配置（LLM/TTS 需真实 key）")
    else:
        print(f"链路事件共 {len(events_log)} 条（日志中 [DanmakuPipeline] 可见完整编排）")
        print("若含 字幕 + TTS audio_ready + Live2D 口型 三项，则 P1 链路代码级跑通")
    print("=" * 60)


if __name__ == "__main__":
    main()
