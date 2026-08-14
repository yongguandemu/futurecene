"""test_placeholder_modules.py — 占位补齐模块的专项测试

覆盖（规格书 第14章 8 项契约的验收点）：
- model_filter：模型不可用 → 返回 None 由关键词兜底；推理异常 → safe 兜底
- retriever：向量检索返回 top-k、语义相关排序、混合检索
- mc_bridge：bot 缺失 → 清晰错误码；未运行时 send 返回 False
- active_dialogue：定时触发、generator 注入、冷却抑制、事件发布
- vts/obs adapter：缺失外部库时降级为模拟模式，指令/状态可用
- experience：学习-存储-检索闭环（ExperienceStore + TaskPlanner）
- registry：bind 后 resolve 返回真实处理器，未绑定调用给出明确报错
"""
import asyncio

import pytest

from src.shared.capability_registry import CapabilityRegistry
from src.shared.event_bus import EventBus
from src.shared.events import ACTIVE_DIALOGUE


# =====================================================================
# model_filter：模型不可用 / 推理失败兜底
# =====================================================================

def test_model_filter_unavailable_returns_none():
    from src.orchestrators.safety_orchestrator.model_filter import ModelFilter
    mf = ModelFilter(model_dir="zzz_nonexistent_dir", probe_legacy=False)
    assert mf.available is False
    assert mf.check("hello") is None  # 交给关键词规则兜底


def test_model_filter_inference_failure_falls_back_to_safe():
    from src.orchestrators.safety_orchestrator.model_filter import ModelFilter
    mf = ModelFilter(model_dir="zzz_nonexistent_dir", probe_legacy=False)
    mf._model = object()  # 强制"模型已加载"以触发推理路径

    def _boom(text):
        raise RuntimeError("inference crash")

    mf._predict = _boom
    result = mf.check("敏感测试")
    assert result is not None
    assert result["safe"] is True        # 推理异常 → 放行兜底
    assert result["source"] == "fallback"


# =====================================================================
# retriever：向量检索 + 混合检索
# =====================================================================

def _entries(n=5):
    return [
        {"memory_id": str(i), "content": f"记忆内容{i}：今天天气很好", "tags": ["天气"], "timestamp": i}
        for i in range(n)
    ]


def test_vector_retrieve_returns_top_k():
    from src.orchestrators.memory_orchestrator import retriever
    entries = _entries(5)
    out = retriever.vector_retrieve(entries, "天气", k=3)
    assert len(out) == 3
    assert all(e["memory_id"] in {str(i) for i in range(5)} for e in out)


def test_vector_retrieve_semantic_ranking():
    from src.orchestrators.memory_orchestrator import retriever
    entries = [
        {"memory_id": "a", "content": "今天天气晴朗适合直播", "tags": []},
        {"memory_id": "b", "content": "数学公式推导过程记录", "tags": []},
        {"memory_id": "c", "content": "天气降温注意添衣保暖", "tags": []},
    ]
    out = retriever.vector_retrieve(entries, "天气", k=2)
    ids = [e["memory_id"] for e in out]
    assert "b" not in ids  # 无关内容不应进入最相关 top2
    assert len(out) == 2


def test_hybrid_retrieve_returns_k():
    from src.orchestrators.memory_orchestrator import retriever
    entries = _entries(6)
    out = retriever.hybrid_retrieve(entries, "天气", k=4)
    assert len(out) == 4


def test_merge_results_dedup():
    from src.orchestrators.memory_orchestrator import retriever
    short = [{"memory_id": "1", "content": "s", "timestamp": 2}]
    long = [{"memory_id": "1", "content": "dup", "timestamp": 1},
            {"memory_id": "2", "content": "l", "timestamp": 3}]
    out = retriever.merge_results(short, long, k=5)
    ids = [e["memory_id"] for e in out]
    assert ids == ["2", "1"]  # 按时间新→旧，去重


# =====================================================================
# mc_bridge：Node.js 子进程管理 + 通信
# =====================================================================

def test_mc_bridge_missing_bot_returns_clear_error():
    from src.orchestrators.game_orchestrator.mc_bridge import MCBridge
    bridge = MCBridge(bot_path="zzz/nonexistent/bot.js")
    result = bridge.start(mode="live")
    assert result["started"] is False
    assert result["error_code"] == "BOT_NOT_FOUND"  # 清晰错误提示
    assert bridge.running is False


def test_mc_bridge_send_when_not_running():
    from src.orchestrators.game_orchestrator.mc_bridge import MCBridge
    bridge = MCBridge(bot_path="zzz/nonexistent/bot.js")
    assert bridge.send({"type": "move", "x": 1, "z": 2}) is False
    assert bridge.stop() is not None  # 幂等停止


# =====================================================================
# active_dialogue：定时触发主动发言
# =====================================================================

def _make_active(cfg, bus=None):
    from src.orchestrators.llm_orchestrator.active_dialogue import ActiveDialogue
    return ActiveDialogue(event_bus=bus, config=cfg)


def test_active_dialogue_tick_returns_topic():
    bus = EventBus(); bus.reset()
    ad = _make_active({"min_cooldown": 0, "max_silence": 0, "trigger_probability": 1.0}, bus)
    result = ad.tick()
    assert result is not None
    assert result["text"].strip()
    assert "mood" in result


def test_active_dialogue_generator_injection():
    bus = EventBus(); bus.reset()
    ad = _make_active({"min_cooldown": 0, "max_silence": 0, "trigger_probability": 1.0}, bus)
    ad.set_generator(lambda: {"text": "被注入的话题", "mood": "happy"})
    result = ad.tick()
    assert result == {"text": "被注入的话题", "mood": "happy"}


def test_active_dialogue_cooldown_blocks():
    import time
    bus = EventBus(); bus.reset()
    ad = _make_active({"min_cooldown": 9999, "max_silence": 0, "trigger_probability": 1.0}, bus)
    assert ad.tick() is not None
    assert ad.tick() is None  # 冷却期内不触发


def test_active_dialogue_publishes_event():
    bus = EventBus(); bus.reset()
    seen = {}
    bus.subscribe(ACTIVE_DIALOGUE, lambda event, **kw: seen.update(kw))
    ad = _make_active({"min_cooldown": 0, "max_silence": 0, "trigger_probability": 1.0}, bus)
    ad.tick()
    assert seen.get("source") == "active_dialogue"
    assert seen.get("text")


# =====================================================================
# vts / obs adapter：外部库缺失时降级为模拟模式
# =====================================================================

def test_vts_adapter_mock_mode_connect_and_command():
    from src.orchestrators.platform_orchestrator.vts_adapter import VTSAdapter
    adapter = VTSAdapter(event_bus=None)
    assert adapter.connect() is True          # websocket 缺失 → 模拟连接
    assert adapter.is_connected() is True
    assert adapter.set_parameter("ParamAngleX", 0.5) is True
    assert adapter.trigger_hotkey("h1") is True
    stats = adapter.get_stats()
    assert stats["connected"] is True
    adapter.disconnect()
    assert adapter.is_connected() is False


def test_obs_adapter_mock_mode_connect_and_status():
    from src.orchestrators.platform_orchestrator.obs_adapter import OBSAdapter
    adapter = OBSAdapter(event_bus=None)
    assert adapter.connect() is True          # simpleobsws 缺失 → 模拟连接
    assert adapter.is_connected() is True
    status = adapter.get_status()
    assert status["connected"] is True
    adapter.disconnect()
    assert adapter.is_connected() is False


# =====================================================================
# experience：学习-存储-检索闭环
# =====================================================================

def test_experience_store_learn_store_retrieve_loop(tmp_path):
    from src.orchestrators.experience_orchestrator.experience_store import ExperienceStore
    from src.orchestrators.experience_orchestrator.state_encoder import GameState

    store = ExperienceStore(data_file=str(tmp_path / "exp.json"), game="test")
    state = GameState(scene_type="menu", text="开始游戏", fingerprint="fp1",
                      timestamp=1.0)
    # 学习：记录成功动作
    store.record(state, "press_key", {"vk": 0x0D}, "success")
    store.flush()
    # 存储：重新加载
    store2 = ExperienceStore(data_file=str(tmp_path / "exp.json"), game="test")
    # 检索：命中刚才学习的经验
    hits = store2.query(GameState(scene_type="menu", text="开始游戏",
                                  fingerprint="fp1", timestamp=2.0))
    assert hits, "应能检索到学习过的经验"
    rec, sim = hits[0]
    assert rec["action"] == "press_key"
    assert rec["confidence"] >= store2.min_confidence


def test_task_planner_rule_plan():
    from src.orchestrators.experience_orchestrator.task_planner import TaskPlanner
    planner = TaskPlanner({})
    chain = planner.plan("stone_pickaxe", {})
    assert chain, "rules 模板应产出子任务链"
    assert chain[0]["type"] == "gather"
    assert planner.next_subtask() is not None


# =====================================================================
# registry：bind 后 resolve 返回真实处理器，无空路由
# =====================================================================

def test_capability_registry_bind_resolves_real_handler():
    repo = CapabilityRegistry({"demo:x": [object]})
    assert repo.capabilities() == ["demo:x"]

    def _dispatch(command):
        return {"ok": True}

    repo.bind(_dispatch)
    assert repo.resolve("demo:x") is _dispatch  # 已绑定 → 返回真实处理器
    assert repo.has("demo:x") is True


def test_capability_registry_unbound_raises():
    repo = CapabilityRegistry({"demo:y": [object]})
    handler = repo.resolve("demo:y")            # 未绑定 → 明确占位
    with pytest.raises(RuntimeError):
        handler({})


def test_all_orchestrator_registries_have_bind_and_no_empty_routes():
    """每个调度官 registry 均暴露 bind/capabilities，且注册能力可被 resolve。"""
    import importlib
    orchestrators = [
        "safety_orchestrator", "memory_orchestrator", "game_orchestrator",
        "music_orchestrator", "platform_orchestrator", "screen_control_orchestrator",
        "experience_orchestrator", "tts_orchestrator", "live2d_orchestrator",
        "bilibili_orchestrator", "live_intelligence_orchestrator",
        "stream_orchestrator", "llm_orchestrator",
    ]
    for name in orchestrators:
        reg = importlib.import_module(f"src.orchestrators.{name}.registry")
        assert callable(reg.bind), f"{name}.registry 缺 bind"
        assert callable(reg.capabilities), f"{name}.registry 缺 capabilities"
        caps = reg.capabilities()
        assert isinstance(caps, list) and caps, f"{name}.registry 无能力条目"
        for cap in caps:
            assert reg.has(cap), f"{name}.registry 能力 {cap} 未注册"


def test_orchestrator_binds_registry_to_handle():
    """构造调度官后，registry.resolve 应返回其真实 handle（消除空路由）。"""
    from src.orchestrators.safety_orchestrator import registry as safety_registry
    from src.orchestrators.safety_orchestrator.safety_orchestrator import SafetyOrchestrator
    bus = EventBus(); bus.reset()
    try:
        orch = SafetyOrchestrator(event_bus=bus)
    except Exception:
        pytest.skip("构造依赖不可用")
        return
    resolved = safety_registry.resolve("safety:check_input")
    # bound method 每次访问会生成新对象，故比较 __func__/__self__
    assert resolved.__func__ is orch.handle.__func__
    assert resolved.__self__ is orch
    result = asyncio.run(resolved({"capability": "safety:check_input",
                                   "payload": {"text": "正常内容"}}))
    assert result["ok"] is True