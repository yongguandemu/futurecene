"""test_game_operation_loop.py — 通用游戏操作循环/安全护栏/规划器单测

覆盖：GameOperationController（使能状态机）、OperationSafety（熔断/防抖/冷却）、
GameOperationLoop（感知→判断→操作→反馈闭环 + 重试 + 图像差分）、
GameOperationPlanner（模板 + LLM 双路径）。
"""
import os
import tempfile
import time

from src.orchestrators.game_orchestrator.game_operation_controller import (
    GameOperationController,
    OperationSafety,
)
from src.orchestrators.game_orchestrator.game_operation_loop import GameOperationLoop
from src.orchestrators.game_orchestrator.game_operation_planner import (
    GameOperationPlanner,
)
from src.shared.event_bus import EventBus


# ---------- GameOperationController ----------

def test_controller_starts_disabled():
    c = GameOperationController()
    assert c.enabled is False


def test_controller_start_stop():
    c = GameOperationController()
    snap = c.start(source="manual")
    assert c.enabled is True and snap["source"] == "manual"
    snap = c.stop()
    assert c.enabled is False


def test_controller_auto_stop_after_seconds():
    c = GameOperationController()
    c.start(source="test", stop_after_seconds=0.01)
    time.sleep(0.05)
    assert c.enabled is False  # status() 触发到时自动停


# ---------- OperationSafety ----------

def test_safety_allow_initial():
    s = OperationSafety({"post_action_cooldown": 0})
    assert s.allow("advance") is True


def test_safety_dedup_blocks_same_action():
    s = OperationSafety({"dedup_window": 10, "post_action_cooldown": 0})
    now = 1000.0
    assert s.allow("advance", now) is True
    s.mark_action("advance", now)
    assert s.allow("advance", now + 1) is False  # 同指令去重窗口内拦截
    assert s.allow("advance", now + 11) is True  # 窗口外放行


def test_safety_cooldown_blocks_any_action():
    s = OperationSafety({"post_action_cooldown": 5, "dedup_window": 0})
    now = 1000.0
    assert s.allow("advance", now) is True
    s.mark_action("advance", now)
    assert s.allow("click", now + 1) is False  # 冷却期内任何操作拦截
    assert s.allow("click", now + 6) is True


def test_safety_fuse_blocks_after_no_response():
    s = OperationSafety({"fuse_limit": 2, "fuse_pause": 60, "post_action_cooldown": 0})
    now = 1000.0
    assert s.on_result(ok=True, scene_changed=False, now=now) is False
    fused = s.on_result(ok=True, scene_changed=False, now=now + 1)
    assert fused is True
    assert s.allow("advance", now + 2) is False  # 熔断期内拦截
    assert s.allow("advance", now + 61) is True  # 熔断期后恢复


def test_safety_fuse_resets_on_success():
    s = OperationSafety({"fuse_limit": 3, "post_action_cooldown": 0})
    now = 1000.0
    s.on_result(ok=True, scene_changed=False, now=now)
    s.on_result(ok=True, scene_changed=False, now=now + 1)
    assert s.on_result(ok=True, scene_changed=True, now=now + 2) is False
    assert s.snapshot()["fuse_count"] == 0


# ---------- GameOperationLoop ----------

def _make_loop(scenes, act_fn=None, cfg=None):
    """构造循环。scenes: 每次感知依次返回的场景列表（耗尽后重复最后一个）。"""
    bus = EventBus()
    bus.reset()
    state = {"i": 0}

    def perceive():
        i = min(state["i"], len(scenes) - 1)
        state["i"] += 1
        return scenes[i]

    controller = GameOperationController(cfg)
    controller.start()
    safety = OperationSafety(cfg)
    loop = GameOperationLoop(controller, safety, perceive,
                             act_fn or (lambda a, p: {"ok": True, "scene_changed": True}),
                             event_bus=bus, config=cfg)
    return loop, controller, bus


def test_loop_disabled_no_action():
    bus = EventBus()
    bus.reset()
    controller = GameOperationController()
    loop = GameOperationLoop(controller, OperationSafety(), lambda: {"text": ""},
                             lambda a, p: {"ok": True})
    r = loop.run_cycle()
    assert r["action"] == "no_action" and r["reason"] == "disabled"


def test_loop_dialogue_pause_advances():
    scenes = [{"text": "你好", "state": "dialogue"},
              {"text": "你好", "state": "dialogue"}]
    acts = []
    loop, _, _ = _make_loop(scenes, lambda a, p: acts.append(a) or
                            {"ok": True, "scene_changed": True},
                            cfg={"advance_wait": 0.01, "poll_interval": 0.01})
    # 第一轮：文本变化 → 记录稳定起点
    r1 = loop.run_cycle()
    assert r1["action"] == "no_action"
    time.sleep(0.02)
    # 第二轮：停顿超时 → 推进
    r2 = loop.run_cycle()
    assert r2["action"] == "advance"
    assert acts == ["advance"]


def test_loop_menu_enters():
    loop, _, _ = _make_loop([{"text": "new game", "state": "menu"}])
    r = loop.run_cycle()
    assert r["action"] == "advance"


def test_loop_options_selects():
    loop, _, _ = _make_loop([{"text": "", "state": "dialogue",
                              "options": ["选项A", "选项B"]}])
    r = loop.run_cycle()
    assert r["action"] == "select_option"
    assert r["params"]["index"] == 0


def test_loop_no_text_no_action():
    loop, _, _ = _make_loop([{"text": "", "state": "unknown"}])
    r = loop.run_cycle()
    assert r["action"] == "no_action"


def test_loop_retries_on_failure():
    """操作失败后按 retry_limit 重试，最终成功。"""
    calls = {"n": 0}

    def act(a, p):
        calls["n"] += 1
        if calls["n"] < 3:
            return {"ok": False}
        return {"ok": True, "scene_changed": True}

    loop, _, _ = _make_loop([{"text": "菜单", "state": "menu"}], act,
                            cfg={"retry_limit": 3, "poll_interval": 0.01})
    r = loop.run_cycle()
    assert r["ok"] is True
    assert calls["n"] >= 3  # 失败后重试


def test_loop_max_failures_stops():
    """连续失败超过 max_failures 自动停止。"""
    loop, controller, bus = _make_loop(
        [{"text": "菜单", "state": "menu"}],
        lambda a, p: {"ok": False},
        cfg={"max_failures": 2, "retry_limit": 0, "poll_interval": 0.01,
             "dedup_window": 0, "post_action_cooldown": 0})
    loop.run_cycle()
    assert controller.enabled is True
    loop.run_cycle()
    assert controller.enabled is False  # 连续 2 次失败自动停


def test_loop_publishes_events():
    bus = EventBus()
    bus.reset()
    seen = {}
    bus.subscribe("game:op_operation", lambda event, **kw: seen.update(kw))
    controller = GameOperationController()
    controller.start()
    loop = GameOperationLoop(controller, OperationSafety(),
                             lambda: {"text": "菜单", "state": "menu"},
                             lambda a, p: {"ok": True, "scene_changed": True},
                             event_bus=bus, config={"poll_interval": 0.01})
    loop.run_cycle()
    assert seen.get("action") == "advance"


def test_loop_image_diff_feedback():
    """图像差分反馈：操作后画面变化被感知为 scene_changed。"""
    d = tempfile.mkdtemp()
    img_a = os.path.join(d, "a.png")
    img_b = os.path.join(d, "b.png")
    try:
        from PIL import Image
        Image.new("RGB", (100, 100), (0, 0, 0)).save(img_a)
        Image.new("RGB", (100, 100), (255, 255, 255)).save(img_b)
    except ImportError:
        return  # PIL 缺失时跳过

    scenes = [{"text": "", "state": "menu", "image_path": img_a},
              {"text": "", "state": "menu", "image_path": img_b}]
    loop, _, _ = _make_loop(scenes, lambda a, p: {"ok": True, "scene_changed": True},
                            cfg={"image_diff_threshold": 0.02, "poll_interval": 0.01})
    # 第一轮：菜单 → 推进操作 → 反馈感知到 img_a→img_b 图像变化
    r = loop.run_cycle()
    assert r["action"] == "advance"
    assert r["scene_changed"] is True


def test_loop_experience_and_commentary_linkage():
    """联动：操作成功且场景变化 → 经验记录 + 解说请求各触发一次。"""
    bus = EventBus()
    bus.reset()
    exp = []
    comm = []
    state = {"i": 0}
    scenes = [{"text": "菜单", "state": "menu"},
              {"text": "新的对白", "state": "dialogue"}]

    def perceive():
        i = min(state["i"], len(scenes) - 1)
        state["i"] += 1
        return scenes[i]

    controller = GameOperationController({"dedup_window": 0, "post_action_cooldown": 0})
    controller.start()
    loop = GameOperationLoop(controller, OperationSafety({"dedup_window": 0,
                                                          "post_action_cooldown": 0}),
                             perceive,
                             lambda a, p: {"ok": True, "scene_changed": True},
                             event_bus=bus,
                             experience_fn=lambda a, p, s: exp.append(a),
                             commentary_fn=lambda a, s: comm.append(a),
                             config={"poll_interval": 0.01})
    loop.run_cycle()
    assert exp == ["advance"]
    assert comm == ["advance"]


def test_loop_no_linkage_when_no_scene_change():
    """联动：操作成功但场景无变化 → 不记录经验、不请求解说。"""
    bus = EventBus()
    bus.reset()
    exp = []
    comm = []
    controller = GameOperationController()
    controller.start()
    loop = GameOperationLoop(controller, OperationSafety(),
                             lambda: {"text": "菜单", "state": "menu"},
                             lambda a, p: {"ok": True, "scene_changed": False},
                             event_bus=bus,
                             experience_fn=lambda a, p, s: exp.append(a),
                             commentary_fn=lambda a, s: comm.append(a),
                             config={"poll_interval": 0.01})
    loop.run_cycle()
    assert exp == [] and comm == []


# ---------- GameOperationPlanner ----------

def test_planner_template_match():
    p = GameOperationPlanner()
    plan = p.generate_plan("向前走")
    assert plan == [{"action": "hold", "params": {"key": "W"}}]


def test_planner_empty_command():
    p = GameOperationPlanner()
    assert p.generate_plan("") == []


def test_planner_llm_fallback():
    p = GameOperationPlanner(chat_fn=lambda prompt: '[{"action": "keypress", "params": {"key": "E"}}]')
    plan = p.generate_plan("互动一下")
    assert plan == [{"action": "keypress", "params": {"key": "E"}}]


def test_planner_llm_invalid_json():
    p = GameOperationPlanner(chat_fn=lambda prompt: "不是 JSON")
    assert p.generate_plan("随便") == []


def test_planner_validate_plan():
    ok, err = GameOperationPlanner.validate_plan([{"action": "click", "params": {"x": 1, "y": 2}}])
    assert ok is True
    ok, err = GameOperationPlanner.validate_plan([{"action": "fly"}])
    assert ok is False and "未知动作" in err
    ok, err = GameOperationPlanner.validate_plan([{"action": "click", "params": {}}])
    assert ok is False and "坐标" in err
