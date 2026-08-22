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
from src.orchestrators.schedule_orchestrator import ScheduleOrchestrator  # noqa: E402
from src.orchestrators.screen_control_orchestrator import ScreenControlOrchestrator  # noqa: E402
from src.orchestrators.stream_orchestrator import StreamOrchestrator  # noqa: E402
from src.orchestrators.stream_orchestrator.headless_streamer import HeadlessStreamer  # noqa: E402
from src.orchestrators.stream_orchestrator.stream_code_refresher import StreamCodeRefresher  # noqa: E402
from src.orchestrators.tts_orchestrator import TTSOrchestrator  # noqa: E402
from src.shared.config_loader import ConfigLoader, load  # noqa: E402
from src.shared.crash_reporter import CrashReporter  # noqa: E402
from src.shared.decision_log import attach as attach_decision_log  # noqa: E402
from src.shared.events import ACTIVE_DIALOGUE, DANMAKU_RECEIVED  # noqa: E402
from src.shared.decision_log import clear_log, log_stats, recent_entries  # noqa: E402
from src.shared.event_bus import EventBus  # noqa: E402
from src.shared.logger import get  # noqa: E402
from src.shared.watchdog import Watchdog  # noqa: E402
from src.web.app_factory import create_app  # noqa: E402

logger = get("app")


def _role_topic(role: str, profiles, session, collaboration, llm_orch) -> dict:
    """角色化主动话题生成（Task 18 冷场自发闲聊）。

    fn(role) -> {text, mood}（ActiveDialogue.set_role_generator 契约）：
    prompt 携带角色人设（profile.system_prompt）+ 话题偏好（behavior_rules）
    + 对方最近发言（collaboration snapshot.recent_turns）+ 在场搭档
    （session.present_roles），调 LLM _chat 生成；
    任何失败回退 DEFAULT_TOPICS 话题池随机，保证冷场闲聊不中断。
    """
    import random
    from src.orchestrators.llm_orchestrator.active_dialogue import DEFAULT_TOPICS
    try:
        p = profiles.load(role) if profiles is not None else None
        persona = getattr(p, "system_prompt", "") if p else ""
        # 世界书设定块（与 danmaku_pipeline._system_prompt 对齐）：主动对话同样注入
        # 世界设定，避免冷场闲聊脱离世界观。
        from src.shared.world_book import get_world_book
        wb_block = get_world_book().system_prompt_block(role)
        if wb_block:
            persona = (persona + "\n\n" + wb_block).strip() if persona else wb_block
        # 话题偏好（behavior_rules.yaml → preferred_topics / avoid_topics）
        topic_hint = ""
        if p is not None:
            rules = getattr(p, "behavior_rules", {}) or {}
            preferred = rules.get("rules", {}).get("preferred_topics") or []
            avoid = rules.get("rules", {}).get("avoid_topics") or []
            parts = []
            if preferred:
                parts.append("擅长话题：" + "、".join(str(t) for t in preferred))
            if avoid:
                parts.append("回避话题：" + "、".join(str(t) for t in avoid))
            topic_hint = "；".join(parts) + "。" if parts else ""
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
            "{topic_hint}{companions}{partner}回复控制在两句话以内，不要使用表情符号。"
        ).format(role=role, topic_hint=topic_hint, companions=companions, partner=partner)
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
    attach_decision_log(event_bus)      # 决策日志接入事件总线（decision:logged）
    switch_manager = SwitchManager(event_bus)
    registry = OrchestratorRegistry(switch_manager, event_bus)
    session = SessionContext(session_id="default")
    session.bind_event_bus(event_bus)

    # ---------- 用户设置（任务五：设置面板持久化 data/config_user.json） ----------
    from src.shared.user_config import UserConfigStore
    user_config = UserConfigStore()

    # ---------- 调度官（注册即生成开关 + start） ----------
    config_loader = ConfigLoader()
    p2_music_cfg = config_loader.get("music") or {}
    p2_platform_cfg = config_loader.get("platform") or {}
    p2_stream_cfg = config_loader.get("stream") or {}
    p2_exp_cfg = config_loader.get("experience") or {}
    p2_game_cfg = config_loader.get("game") or {}
    p1_intel_cfg = config_loader.get("intelligence") or {}
    schedule_cfg = config_loader.get("schedule") or {}
    streamer = HeadlessStreamer(page_url=p2_stream_cfg.get("page_url",
                                                           "http://127.0.0.1:8000/live2d-stream.html?paused=0"),
                                width=p2_stream_cfg.get("width", 1280),
                                height=p2_stream_cfg.get("height", 720),
                                fps=p2_stream_cfg.get("fps", 12),
                                bitrate=p2_stream_cfg.get("bitrate", "800k"))
    refresher = StreamCodeRefresher(room_id=p2_stream_cfg.get("room_id", 0),
                                    identity_code=p2_stream_cfg.get("identity_code", ""))
    # screen 先实例化，绑定到 game（通用游戏操作经 screen 命令调用感知/操作）
    screen_orch = ScreenControlOrchestrator(
        event_bus=event_bus,
        vision_api_key=os.environ.get("GLMVISION_API_KEY", ""))
    game_orch = GameOrchestrator(event_bus=event_bus,
                                 screen_orchestrator=screen_orch,
                                 config=p2_game_cfg)
    orchestrators = [
        LLMOrchestrator(event_bus=event_bus, config_loader=config_loader),
        TTSOrchestrator(event_bus=event_bus, config_loader=config_loader),
        Live2DOrchestrator(event_bus=event_bus),
        BilibiliOrchestrator(event_bus=event_bus, config_loader=config_loader),
        MemoryOrchestrator(event_bus=event_bus,
                           switch_check=lambda name: switch_manager.is_enabled(name),
                           strength_provider=lambda: user_config.get("memory_strength")),
        SafetyOrchestrator(event_bus=event_bus),
        screen_orch,
        game_orch,
        # ---------- P2 扩展调度官 ----------
        MusicOrchestrator(event_bus=event_bus, config=p2_music_cfg),
        PlatformOrchestrator(event_bus=event_bus, config=p2_platform_cfg),
        StreamOrchestrator(event_bus=event_bus, config=p2_stream_cfg,
                           refresher=refresher, streamer=streamer),
        ExperienceOrchestrator(event_bus=event_bus, config=p2_exp_cfg),
        # ---------- P1 直播间智能（精细子模块） ----------
        LiveIntelligenceOrchestrator(event_bus=event_bus, config=p1_intel_cfg),
        # ---------- 日程调度（P0 补迁：排期到点触发动作） ----------
        ScheduleOrchestrator(event_bus=event_bus, config=schedule_cfg),
    ]
    for orch in orchestrators:
        registry.register(orch)

    # 通用游戏操作联动接线：LLM 规划（llm:chat）+ 经验学习（experience:feedback）
    game_orch.set_llm_orchestrator(registry.get("llm"))
    game_orch.set_experience_orchestrator(registry.get("experience"))

    # 世界书自动进化：订阅弹幕/礼物事件 → 生成观众话题/重要观众/常驻观众条目（D3：装配层启动）
    from src.shared.world_book import get_world_book
    get_world_book().start_evolving(
        event_bus,
        config_loader.get("world_book", {}).get("evolve", {}) or {})

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
    if tts_orch is not None:
        tts_orch.start()  # 装配层启动：创建 wusound/cosyvoice 引擎（D3 被动工作，由装配层启动）
    memory_orch = registry.get("memory")
    from src.commander.tool_registry import ToolRegistry
    tool_registry = ToolRegistry()  # LLM 工具注册表（内置世界书查询/系统状态）
    pipeline = DanmakuPipeline(event_bus=event_bus, llm_orchestrator=llm_orch,
                               tts_orchestrator=tts_orch,
                               memory_orchestrator=memory_orch,
                               switch_manager=switch_manager,
                               session=session,
                               profile_loader=profile_loader,
                               tool_registry=tool_registry)
    pipeline.start()

    # ---------- input 域（总控调度化，规格 2026-08-22 任务一） ----------
    from src.commander.input import (
        InputClassifier, PriorityQueue, DistributionRouter, ContextAggregator)
    input_classifier = InputClassifier()
    input_queue = PriorityQueue()
    distribution_router = DistributionRouter(
        intent_parser=intent_parser, command_router=command_router,
        danmaku_pipeline=pipeline, event_bus=event_bus)
    context_aggregator = ContextAggregator(
        memory=memory_orch, session=session, event_bus=event_bus)
    # 总控分发模式开关：默认关（direct 直通，现有链路不变）；开 = priority 排队
    switch_manager.auto_register("input_dispatch", default=False)

    # ---------- 任务二：发言时间线调度（SpeechScheduler + 批量发言计划开关） ----------
    from src.commander.speech_scheduler import SpeechScheduler
    switch_manager.auto_register("batch_mode", default=False)      # 主动批预生成（默认关，保持现有单条行为）
    switch_manager.auto_register("real_time_mode", default=True)   # 被动发言实时插入（默认开）
    speech_scheduler = SpeechScheduler(
        event_bus=event_bus,
        switch_check=lambda name: switch_manager.is_enabled(name))
    pipeline.set_speech_scheduler(speech_scheduler)

    # ---------- 任务四：分层记忆（开关 + LLM 摘要注入） ----------
    switch_manager.auto_register("memory_compression", default=True)           # 压缩开关（默认开）
    switch_manager.auto_register("allow_memory_to_worldbook", default=False)   # L3→世界书提案（默认关）

    def _memory_summarize(text: str, max_chars: int) -> str:
        """记忆压缩摘要：统一 fast 引擎（deepseek-v4-flash，规格书禁用 glm-4.7-flash）。

        全链失败（error 非空，reply 为兜底回复）→ 返回空串，压缩器降级原文分段。
        """
        if not text or not text.strip():
            return ""
        import asyncio
        try:
            result = asyncio.run(llm_orch.handle({"capability": "llm:chat", "payload": {
                "engine": "fast",
                "system_prompt": (
                    f"你是直播记忆压缩器。把输入的事件流水压缩为不超过 {max_chars} 字的中文摘要，"
                    "保留人物、事件、观众偏好与时间线。只输出摘要正文，不要任何解释。"),
                "text": text[:6000],
            }}))
        except Exception as e:
            logger.warning("[app] 记忆摘要调用异常，降级原文分段: %s", e)
            return ""
        if not result or not result.get("ok") or result.get("error"):
            return ""  # 全链失败：兜底回复不可当摘要
        reply = (result.get("data") or {}).get("reply", "")
        return reply if isinstance(reply, str) else ""

    memory_orch.set_summarize_fn(_memory_summarize)
    logger.info("[app] 任务四装配完成：memory_compression=%s allow_memory_to_worldbook=%s",
                switch_manager.is_enabled("memory_compression"),
                switch_manager.is_enabled("allow_memory_to_worldbook"))

    # ---------- 日程触发分发（P0 补迁）：schedule:fired → 指挥官命令分发 ----------
    # 排期到点时 ScheduleOrchestrator 只发事件；此处由装配层订阅并把动作
    # 投递给指挥官，经正常命令分发链（command_router → 调度官 handle）执行。
    from src.shared.events import SCHEDULE_FIRED

    def _on_schedule_fired(event, action="", payload=None, **kw):
        import asyncio
        from src.commander.intent_parser import Command
        try:
            asyncio.run(command_router.dispatch(
                Command(capability=action, payload=payload or {},
                        source="schedule", session_id=session.session_id)))
        except Exception as e:
            logger.warning("[app] 排期动作分发失败 %s: %s", action, e)

    event_bus.subscribe(SCHEDULE_FIRED, _on_schedule_fired)
    logger.info("[app] 已订阅 schedule:fired 排期分发")

    # ---------- 本机 TTS 播放（直播测试台 · 无前端浏览器场景） ----------
    # 输出目标由用户设置 tts_output_target 决定（任务五 5.2，替代单布尔开关）：
    # local/both → 订阅 tts:audio_ready 播放到本机扬声器；stream → 仅推流不本机播放。
    if user_config.get("tts_output_target") in ("local", "both"):
        from src.orchestrators.tts_orchestrator.local_player import LocalTTSSpeaker
        local_tts = LocalTTSSpeaker(
            event_bus,
            cache_dir=str(getattr(tts_orch, "_cache_dir", "") or ""))
        local_tts.start()

    # ---------- 冷场主动对话（直播测试台 · 主动模式） ----------
    # LLM 调度官内部持有 ActiveDialogue（构造时创建）。遵循"模块傻"原则：
    # 模块不自启动——此处仅绑定 EventBus 与 role_provider，是否随服务启动由
    # config.llm.active_dialogue.enabled 决定（enabled=true 时配置授权启动）；
    # 禁用时（enabled=false/缺省）不启动，仅能经显式指令
    # POST /api/command llm:active_dialogue {action:start} 才运行。
    active_dialogue = getattr(llm_orch, "_active", None)
    if active_dialogue is not None:
        active_dialogue.set_event_bus(event_bus)
        active_dialogue.set_role_provider(lambda: session.role)
        active_dialogue.set_switch_check(lambda name: switch_manager.is_enabled(name))
        llm_ad_cfg = (config_loader.get("llm") or {}).get("active_dialogue", {}) or {}
        if llm_ad_cfg.get("enabled"):
            active_dialogue.start()
            logger.info("[app] ActiveDialogue 按配置 enabled=true 随服务启动（role_provider=session.role）")
        else:
            logger.info("[app] ActiveDialogue 配置禁用，不随服务启动（可经 llm:active_dialogue start 显式开启）")

    # ---------- Live2D 模型装载（直播测试台：使后端模型状态非空） ----------
    # 此前 live2d:load 仅 demo_danmaku.py 调用，生产装配零调用导致后端
    # _models 恒空、lip_sync_start 事件不发出；此处启动时装载当前角色模型。
    live2d_orch = registry.get("live2d")
    if live2d_orch is not None:
        try:
            import asyncio
            load_result = asyncio.run(live2d_orch.handle(
                {"capability": "live2d:load",
                 "payload": {"role": session.role}}))
            logger.info("[app] live2d:load 完成: %s", load_result)
        except Exception as e:
            logger.warning("[app] live2d:load 失败: %s", e)

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

        # V3 判断器（judge: llm 时启用）：LLM 紧迫度裁决（提议-裁决单通道，护栏不变）。
        # LLMJudge 自带预算（budget_per_min 次/分钟）与失败回退规则链，链路不中断。
        judge_obj = None
        if str(collab_cfg.get("judge", "rules")) == "llm":
            from src.orchestrators.collaboration.judge import LLMJudge
            judge_cfg = collab_cfg.get("llm_judge") or {}
            llm_judge_orch = registry.get("llm")
            if llm_judge_orch is not None:
                judge_obj = LLMJudge(
                    llm_judge_orch, collab_profiles,
                    budget_per_min=int(judge_cfg.get("budget_per_min", 4)),
                    rules_order=collab_cfg.get("rules_order"))
                logger.info("[app] collaboration judge=llm（预算 %d 次/分钟，回退规则链）",
                            judge_obj._budget)

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
            judge=judge_obj,
        )
        collaboration.start()

        # 多角色模式下弹幕由协调器仲裁分发（谁回应由规则链决定），
        # 管线不再自行按当前角色处理（避免一条弹幕双重发言）
        event_bus.unsubscribe(DANMAKU_RECEIVED, pipeline._on_danmaku)
        # 主动对话也由协调器处理，避免双重执行（管线说一遍 + 协调器再说一遍）
        event_bus.unsubscribe(ACTIVE_DIALOGUE, pipeline._on_active_dialogue)

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
    else:
        # 非协作模式：同样注入 _role_generator，使主动对话带角色人设（修复 system_prompt 为空）
        if active_dialogue is not None:
            active_dialogue.set_role_generator(
                lambda role: _role_topic(role, profile_loader, session, None, llm_orch))
            logger.info("[app] ActiveDialogue 角色化话题生成器已注入（单角色模式）")

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

    # 成本记录接线（任务五 5.3：用量监控真实数据）——事件驱动，调度官零改动耦合：
    # LLM_RESPONDED 带 usage/model → record("llm")；TTS_REQUESTED 带 text → record("tts", chars)
    from src.shared.events import LLM_RESPONDED, TTS_REQUESTED

    def _on_cost_llm(event, capability="", text="", usage=None, model="", **kw):
        usage = usage or {}
        if usage:
            cost_tracker.record(
                "llm", provider="openai", model=model or "gpt-4o-mini",
                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage.get("completion_tokens", 0) or 0))

    def _on_cost_tts(event, text="", **kw):
        cost_tracker.record("tts", chars=len(text or ""))

    event_bus.subscribe(LLM_RESPONDED, _on_cost_llm, name="CostTracker-LLM")
    event_bus.subscribe(TTS_REQUESTED, _on_cost_tts, name="CostTracker-TTS")
    logger.info("[app] 成本记录接线完成：LLM_RESPONDED / TTS_REQUESTED → CostTracker")

    def metrics_provider():
        return {"cost": {**cost_tracker.snapshot(),
                         "today": cost_tracker.get_stats("today")},
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
        "decision_log": {"recent": recent_entries, "stats": log_stats,
                         "clear": clear_log},
        "world_book": get_world_book(),
        "user_config": user_config,      # 任务五：设置面板
        "memory": memory_orch,           # 任务五：记忆库/审阅 API
    }
    return create_app(context), event_bus


def main() -> None:
    load()  # 缺失必填环境变量时在此退出
    app, _ = build_app_context()
    logger.info("Future Scene 已装配: /api/health /api/state /api/command /ws/events")
    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == "__main__":
    main()
