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


def _role_topic(role: str, profiles, session, collaboration, llm_orch) -> dict:
    """角色化主动话题生成（Task 18 冷场自发闲聊）。

    fn(role) -> {text, mood}（ActiveDialogue.set_role_generator 契约）：
    prompt 携带角色人设（profile.system_prompt）+ 对方最近发言（collaboration
    snapshot.recent_turns）+ 在场搭档（session.present_roles），调 LLM _chat 生成；
    任何失败回退 DEFAULT_TOPICS 话题池随机，保证冷场闲聊不中断。
    """
    import random
    from src.orchestrators.llm_orchestrator.active_dialogue import DEFAULT_TOPICS
    try:
        p = profiles.load(role) if profiles is not None else None
        persona = getattr(p, "system_prompt", "") if p else ""
        partner = ""
        if collaboration is not None:
            turns = collaboration.snapshot().get("recent_turns", []) or []
            others = [t for t in turns if t.get("role") != role and t.get("text")]
            if others:
                partner = "对方最近发言：" + str(others[-1].get("text", ""))[:80]
        present = sorted(session.present_roles) if session is not None else []
        companions = "在场搭档：" + "、".join(r for r in present if r != role)
        prompt = (
            "直播间有些冷场，请以{role}的身份主动发起一个轻松闲聊话题。"
            "{companions}{partner}回复控制在两句话以内，不要使用表情符号。"
        ).format(role=role, companions=companions, partner=partner)
        resp = llm_orch._chat({"text": prompt, "system_prompt": persona, "history": []})
        text = ((resp or {}).get("data") or {}).get("reply", "") or ""
        if text and text.strip():
            return {"text": text.strip(), "mood": "default"}
    except Exception:
        logger.warning("[Collab] 角色化话题生成失败，回退话题池 role=%s", role,
                       exc_info=True)
    return dict(random.choice(DEFAULT_TOPICS))


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

    # ---------- 多角色协作（collaboration.enabled 开关，默认关） ----------
    # 开启条件：COLLAB_ENABLED=1（环境变量优先）或 config.yaml collaboration.enabled=true。
    # 装配 CollaborationCoordinator：订阅 danmaku/speech/completed 并驱动 pipeline.execute_with
    # 按角色发言；profiles 复用既有 profile_loader；add_role 全部合法角色（在场模型）。
    collaboration = None
    collab_profiles = None
    if str(os.environ.get("COLLAB_ENABLED", "") or
           config_loader.get("collaboration.enabled", False)).lower() in ("1", "true"):
        from src.orchestrators.collaboration.coordinator import CollaborationCoordinator

        collab_profiles = profile_loader
        collab_cfg = config_loader.get("collaboration", {}) or {}
        for r in collab_profiles.all_roles():
            session.add_role(r)
        collaboration = CollaborationCoordinator(
            event_bus=event_bus,
            pipeline=pipeline,
            profiles=collab_profiles,
            session=session,
            live2d=registry.get("live2d"),
            lead_role=str(collab_cfg.get("lead_role", "yuki")),
            rules_order=collab_cfg.get("rules_order"),
            trigger_probability=float(collab_cfg.get("trigger_probability", 0.3)),
            trigger_global_cooldown=float(collab_cfg.get("trigger_global_cooldown", 20.0)),
            awareness_enabled=bool((collab_cfg.get("awareness") or {}).get("enabled", True)),
        )
        collaboration.start()

        # ---------- 冷场自发闲聊（Task 18）：active_dialogue 角色化 ----------
        # LLM 调度官内部持有 ActiveDialogue 实例（llm_orchestrator._active，构造时已注入
        # 通用 set_generator）；协作开启时追加 set_role_generator(fn)，使 tick(role) 可
        # 生成带角色人设 + 对方最近发言的冷场话题。fn(role) -> {text, mood}，LLM 生成
        # 失败回退 DEFAULT_TOPICS 话题池（见 _role_topic 注释）。
        llm_orch = registry.get("llm")
        active_dialogue = getattr(llm_orch, "_active", None) if llm_orch is not None else None
        if active_dialogue is not None:
            active_dialogue.set_role_generator(
                lambda role: _role_topic(role, collab_profiles, session, collaboration,
                                         llm_orch))

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

    def characters_provider():
        """角色在场快照：在场角色 + 说话中标识（collaboration 装配时叠加）。"""
        chars = {}
        for r in session.present_roles:
            chars[r] = {"present": True}
        if collaboration is not None:
            snap = collaboration.snapshot()
            for r, item in chars.items():
                item["speaking"] = (snap.get("current_speaker") == r)
        return chars

    state_provider = StateProvider(
        event_bus=event_bus,
        session=session,
        switch_manager=switch_manager,
        registry=registry,
        degradation_manager=degradation,
        metrics_provider=metrics_provider,
        characters_provider=characters_provider,
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
        "collaboration": collaboration,
        "profiles": collab_profiles if collaboration else None,
    }
    return create_app(context), event_bus


def main() -> None:
    load()  # 缺失必填环境变量时在此退出
    app, _ = build_app_context()
    logger.info("Future Scene 已装配: /api/health /api/state /api/command /ws/events")
    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == "__main__":
    main()
