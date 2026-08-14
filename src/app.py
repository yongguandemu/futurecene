"""app.py — 应用装配入口（P0-M5 全量装配）

启动流程：
1. 加载 .env 并校验必填环境变量（缺失即退出，规格书 6.2）。
2. 创建 EventBus / SwitchManager / OrchestratorRegistry / SessionContext。
3. 实例化并注册 8 个调度官（llm/tts/live2d/bilibili/memory/safety/game/screen）。
4. 装配 Intent Parser + Command Router + DanmakuPipeline（弹幕→LLM→字幕→TTS）。
5. 启动 Watchdog（注册全部调度官 health）、装配 Cost Tracker + 熔断器 + 降级管理。
6. 创建 Flask 应用（网关端点 + WS + 静态前端），启动 HTTP 服务。

# 模块内容清单（8 项契约）
1. 模块身份标识：app · 应用装配入口 · 对外 build_app_context()/main()
2. 配置契约：.env 必填环境变量校验（缺失即退出，规格书 6.2）；ConfigLoader 各域配置（music/platform/stream/experience/intelligence）
3. 输入契约：无业务输入；读取 .env 与配置装配组件
4. 输出契约：build_app_context() 返回 (Flask app, EventBus)；main() 启动 HTTP 服务（/api/health /api/state /api/command /ws/events）
5. 依赖声明：sys、pathlib、EventBus/SwitchManager/OrchestratorRegistry/SessionContext、全部调度官、IntentParser/CommandRouter/DanmakuPipeline、CostTracker/CostCircuitBreaker/Watchdog/DegradationManager/CrashReporter、create_app
6. 错误定义：必填环境变量缺失即退出；装配异常向上抛出
7. 生命周期方法：main()（启动入口）、build_app_context()（装配）
8. 领域状态说明：context 字典持有全部组件引用（event_bus/switch_manager/registry/session 等）
"""
import os
import sys
from pathlib import Path

# 支持 `python src/app.py` 直接运行：将项目根目录加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.commander.command_router import CommandRouter  # noqa: E402
from src.commander.cost_circuit_breaker import CostCircuitBreaker  # noqa: E402
from src.commander.cost_tracker import CostTracker  # noqa: E402
from src.commander.danmaku_pipeline import DanmakuPipeline  # noqa: E402
from src.commander.degradation_manager import DegradationManager  # noqa: E402
from src.commander.intent_parser import IntentParser  # noqa: E402
from src.commander.orchestrator_registry import OrchestratorRegistry  # noqa: E402
from src.commander.session_context import SessionContext  # noqa: E402
from src.commander.switch_manager import SwitchManager  # noqa: E402
from src.orchestrators.bilibili_orchestrator import BilibiliOrchestrator  # noqa: E402
from src.orchestrators.experience_orchestrator import ExperienceOrchestrator  # noqa: E402
from src.orchestrators.game_orchestrator import GameOrchestrator  # noqa: E402
from src.orchestrators.music_orchestrator import MusicOrchestrator  # noqa: E402
from src.orchestrators.platform_orchestrator import PlatformOrchestrator  # noqa: E402
from src.orchestrators.live2d_orchestrator import Live2DOrchestrator  # noqa: E402
from src.orchestrators.live_intelligence_orchestrator import LiveIntelligenceOrchestrator  # noqa: E402
from src.orchestrators.llm_orchestrator import LLMOrchestrator  # noqa: E402
from src.orchestrators.memory_orchestrator import MemoryOrchestrator  # noqa: E402
from src.orchestrators.safety_orchestrator import SafetyOrchestrator  # noqa: E402
from src.orchestrators.screen_control_orchestrator import ScreenControlOrchestrator  # noqa: E402
from src.orchestrators.stream_orchestrator import StreamOrchestrator  # noqa: E402
from src.orchestrators.stream_orchestrator.headless_streamer import HeadlessStreamer  # noqa: E402
from src.orchestrators.stream_orchestrator.stream_code_refresher import StreamCodeRefresher  # noqa: E402
from src.orchestrators.tts_orchestrator import TTSOrchestrator  # noqa: E402
from src.shared.config_loader import ConfigLoader, load  # noqa: E402
from src.shared.crash_reporter import CrashReporter  # noqa: E402
from src.shared.event_bus import EventBus  # noqa: E402
from src.shared.logger import get  # noqa: E402
from src.shared.watchdog import Watchdog  # noqa: E402
from src.web.app_factory import create_app  # noqa: E402

logger = get("app")


def build_app_context():
    """装配全部组件，返回 Flask 应用与事件总线（便于测试复用）。"""
    event_bus = EventBus()
    switch_manager = SwitchManager(event_bus)
    registry = OrchestratorRegistry(switch_manager, event_bus)
    session = SessionContext(session_id="default")
    session.bind_event_bus(event_bus)

    # ---------- 调度官（注册即生成开关 + start） ----------
    config_loader = ConfigLoader()
    p2_music_cfg = config_loader.get("music") or {}
    p2_platform_cfg = config_loader.get("platform") or {}
    p2_stream_cfg = config_loader.get("stream") or {}
    p2_exp_cfg = config_loader.get("experience") or {}
    p1_intel_cfg = config_loader.get("intelligence") or {}
    streamer = HeadlessStreamer(page_url=p2_stream_cfg.get("page_url",
                                                           "http://127.0.0.1:8000/live2d-stream.html?paused=0"),
                                width=p2_stream_cfg.get("width", 1280),
                                height=p2_stream_cfg.get("height", 720),
                                fps=p2_stream_cfg.get("fps", 12),
                                bitrate=p2_stream_cfg.get("bitrate", "800k"))
    refresher = StreamCodeRefresher(room_id=p2_stream_cfg.get("room_id", 0),
                                    identity_code=p2_stream_cfg.get("identity_code", ""))
    orchestrators = [
        LLMOrchestrator(event_bus=event_bus, config_loader=config_loader),
        TTSOrchestrator(event_bus=event_bus, config_loader=config_loader),
        Live2DOrchestrator(event_bus=event_bus),
        BilibiliOrchestrator(event_bus=event_bus, config_loader=config_loader),
        MemoryOrchestrator(event_bus=event_bus),
        SafetyOrchestrator(event_bus=event_bus),
        ScreenControlOrchestrator(
            event_bus=event_bus,
            vision_api_key=os.environ.get("GLMVISION_API_KEY", "")),
        GameOrchestrator(event_bus=event_bus,
                         screen_orchestrator=None),
        # ---------- P2 扩展调度官 ----------
        MusicOrchestrator(event_bus=event_bus, config=p2_music_cfg),
        PlatformOrchestrator(event_bus=event_bus, config=p2_platform_cfg),
        StreamOrchestrator(event_bus=event_bus, config=p2_stream_cfg,
                           refresher=refresher, streamer=streamer),
        ExperienceOrchestrator(event_bus=event_bus, config=p2_exp_cfg),
        # ---------- P1 直播间智能（精细子模块） ----------
        LiveIntelligenceOrchestrator(event_bus=event_bus, config=p1_intel_cfg),
    ]
    for orch in orchestrators:
        registry.register(orch)

    # ---------- 指挥官 ----------
    from src.commander.character_profile import CharacterProfileLoader
    profile_loader = CharacterProfileLoader()
    intent_parser = IntentParser()
    command_router = CommandRouter(registry, switch_manager, event_bus,
                                   profile_loader=profile_loader,
                                   session=session)

    # ---------- 弹幕 → 对话 → TTS → Live2D 全链路（规格书 9.2） ----------
    llm_orch = registry.get("llm")
    tts_orch = registry.get("tts")
    safety_orch = registry.get("safety")
    memory_orch = registry.get("memory")
    pipeline = DanmakuPipeline(event_bus=event_bus, llm_orchestrator=llm_orch,
                               tts_orchestrator=tts_orch,
                               safety_orchestrator=safety_orch,
                               memory_orchestrator=memory_orch,
                               switch_manager=switch_manager,
                               session=session,
                               profile_loader=profile_loader)
    pipeline.start()

    # ---------- 运维：成本 / 熔断 / 看门狗 / 降级 / 崩溃 ----------
    cost_tracker = CostTracker(event_bus=event_bus)
    cost_breaker = CostCircuitBreaker(event_bus, daily_limit=5.0, monthly_limit=100.0)
    watchdog = Watchdog()
    watchdog.bind_event_bus(event_bus)
    for name in registry.all():
        watchdog.register(name.name, name.health)
    watchdog.start()
    degradation = DegradationManager(switch_manager, event_bus=event_bus)
    crash_reporter = CrashReporter()
    crash_reporter.install()

    def metrics_provider():
        return {"cost": cost_tracker.snapshot(),
                "watchdog": watchdog.get_status(),
                "circuit_breaker": cost_breaker.snapshot()}

    # ---------- 状态快照：StateProvider + StatePublisher（前端重构 · 方案 A） ----------
    from src.commander.state_publisher import StatePublisher
    from src.web.state_provider import StateProvider

    state_provider = StateProvider(
        event_bus=event_bus,
        session=session,
        switch_manager=switch_manager,
        registry=registry,
        degradation_manager=degradation,
        metrics_provider=metrics_provider,
    )
    state_publisher = StatePublisher(event_bus, state_provider)
    state_publisher.start()

    context = {
        "event_bus": event_bus,
        "switch_manager": switch_manager,
        "registry": registry,
        "session": session,
        "intent_parser": intent_parser,
        "command_router": command_router,
        "metrics_provider": metrics_provider,
        "watchdog": watchdog,
        "cost_tracker": cost_tracker,
        "cost_breaker": cost_breaker,
        "degradation_manager": degradation,
        "state_provider": state_provider,
        "state_publisher": state_publisher,
    }
    return create_app(context), event_bus


def main() -> None:
    load()  # 缺失必填环境变量时在此退出
    app, _ = build_app_context()
    logger.info("Future Scene 已装配: /api/health /api/state /api/command /ws/events")
    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == "__main__":
    main()
