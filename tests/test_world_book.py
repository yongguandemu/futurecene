"""test_world_book.py — 世界书加载/检索/注入联通单测

覆盖：WorldBook 加载（472 条迁移数据）、按角色/分类/关键词检索、
system_prompt_block 核心设定块生成、context_aggregator 兼容协议。
"""
import json
import tempfile
from pathlib import Path

from src.shared.world_book import DEFAULT_WORLDBOOK_PATH, WorldBook


def _write_book(path: Path, entries, version=2):
    book = {"version": version, "entry_count": len(entries),
            "entries": entries,
            "categories": {}}
    for e in entries:
        book["categories"].setdefault(e["category"], []).append(e["entry_id"])
    path.write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")


def _sample_entries():
    return [
        {"entry_id": "wb_yuki_identity", "title": "Yuki 的身份",
         "content": "Yuki 是一个刚被唤醒不久的 AI 实习生。",
         "category": "character", "tags": [],
         "metadata": {"role": "yuki"}},
        {"entry_id": "wb_yuki_speaking", "title": "Yuki 的说话风格",
         "content": "句尾偶尔出现「呢」「哦」「呀」。",
         "category": "character", "tags": [],
         "metadata": {"role": "yuki"}},
        {"entry_id": "wb_lilith_identity", "title": "莉莉丝的身份",
         "content": "莉莉丝·奥古斯都是游戏中的 Boss。",
         "category": "character", "tags": [],
         "metadata": {"role": "lilith"}},
        {"entry_id": "wb_viewer_1", "title": "重要观众：吃瓜群众",
         "content": "忠实支持者。", "category": "viewer", "tags": [],
         "metadata": {}},
        {"entry_id": "wb_topic_1", "title": "观众话题：哈哈",
         "content": "观众频繁聊到「哈哈」。", "category": "audience_insight",
         "tags": ["话题", "哈哈"], "metadata": {}},
    ]


def test_load_real_worldbook_migrated():
    """迁移验证：data/worldbook.json 已落盘且 474 条完整加载（472 迁移 + ADR-014 新增 2 条人设唯一性）。"""
    wb = WorldBook(DEFAULT_WORLDBOOK_PATH)
    stats = wb.stats()
    assert stats["total_entries"] == 474
    assert stats["version"] == 2
    # 核心分类齐全
    assert stats["categories"].get("character", 0) >= 19
    assert stats["categories"].get("relationship", 0) >= 6
    assert stats["categories"].get("behavior", 0) >= 5
    # ADR-014：人设唯一性条目已注入（角色边界由正向设定维持）
    for role in ("yuki", "lilith"):
        assert any("人设唯一性" in e["title"] and e["metadata"].get("role") == role
                   for e in wb.core_entries(role))


def test_entries_for_role():
    """按 metadata.role 严格过滤：yuki/lilith 条目不混。"""
    d = tempfile.mkdtemp()
    p = Path(d) / "wb.json"
    _write_book(p, _sample_entries())
    wb = WorldBook(p)
    yuki = wb.entries_for_role("yuki")
    assert {e["title"] for e in yuki} == {"Yuki 的身份", "Yuki 的说话风格"}
    lilith = wb.entries_for_role("lilith")
    assert [e["title"] for e in lilith] == ["莉莉丝的身份"]
    # viewer 条目无 role 字段，不进任何角色
    assert all(e["metadata"].get("role") not in ("yuki", "lilith")
               for e in wb.get_entries_by_category("viewer"))


def test_core_entries_only_character_relationship_behavior():
    """core_entries：仅 character/relationship/behavior 分类 ∩ role。"""
    d = tempfile.mkdtemp()
    p = Path(d) / "wb.json"
    _write_book(p, _sample_entries())
    wb = WorldBook(p)
    yuki_core = wb.core_entries("yuki")
    assert len(yuki_core) == 2  # 两条 character，viewer 不进
    assert all(e["category"] in ("character", "relationship", "behavior")
               for e in yuki_core)


def test_system_prompt_block():
    """核心设定块生成：标题：内容 逐行，含【世界设定】头。"""
    d = tempfile.mkdtemp()
    p = Path(d) / "wb.json"
    _write_book(p, _sample_entries())
    wb = WorldBook(p)
    block = wb.system_prompt_block("yuki")
    assert block.startswith("【世界设定】")
    assert "Yuki 的身份：Yuki 是一个刚被唤醒不久的 AI 实习生。" in block
    assert "莉莉丝" not in block  # 只注入本角色
    # 无匹配角色 → 空串（不注入）
    assert wb.system_prompt_block("nobody") == ""


def test_system_prompt_block_max_chars():
    """超长设定按 max_chars 截断，避免撑爆 system_prompt。"""
    d = tempfile.mkdtemp()
    p = Path(d) / "wb.json"
    _write_book(p, _sample_entries())
    wb = WorldBook(p)
    block = wb.system_prompt_block("yuki", max_chars=20)
    assert len(block) <= 20 + 8  # 头 + 首行边界容差


def test_context_aggregator_compatible_protocol():
    """context_aggregator 消费协议：get_entries_by_category / get_enabled_entries。"""
    d = tempfile.mkdtemp()
    p = Path(d) / "wb.json"
    _write_book(p, _sample_entries())
    wb = WorldBook(p)
    chars = wb.get_entries_by_category("character")
    assert len(chars) == 3
    assert all(isinstance(e, dict) and e["title"] for e in chars)
    enabled = wb.get_enabled_entries()
    assert len(enabled) == 5


def test_missing_file_returns_empty():
    """文件缺失：空书 + 不抛异常（角色 prompt 退回纯档案）。"""
    wb = WorldBook(Path("C:/nonexistent/worldbook.json"))
    assert wb.get_enabled_entries() == []
    assert wb.system_prompt_block("yuki") == ""


# =====================================================================
# CRUD + 持久化（旧系统 WorldBookEvolver 能力）
# =====================================================================

def test_crud_add_update_remove():
    """增删改：add/update（演化日志）/remove。"""
    d = tempfile.mkdtemp()
    p = Path(d) / "wb.json"
    _write_book(p, [])
    wb = WorldBook(p)
    assert wb.add_entry("e1", "标题", "内容", category="character",
                        metadata={"role": "yuki"}) is True
    assert wb.add_entry("e1", "重复", "忽略") is False  # id 冲突
    assert wb.get_entry("e1")["content"] == "内容"
    assert wb.update_entry("e1", content="新内容", reason="手动更新") is True
    assert wb.get_entry("e1")["version"] == 2
    assert wb.get_evolution_log()[0]["action"] == "update"
    assert wb.update_entry("nope", content="x") is False  # 不存在
    assert wb.remove_entry("e1") is True
    assert wb.remove_entry("e1") is False  # 已删除
    assert wb.get_entry("e1") is None


def test_save_to_disk_roundtrip():
    """持久化：add + save_to_disk → 重新加载仍存在。"""
    d = tempfile.mkdtemp()
    p = Path(d) / "wb.json"
    _write_book(p, [])
    wb = WorldBook(p)
    wb.add_entry("e1", "持久化条目", "内容", category="character")
    assert wb.save_to_disk() is True
    wb2 = WorldBook(p)
    assert wb2.get_entry("e1")["title"] == "持久化条目"
    assert wb2.stats()["total_entries"] == 1


def test_get_entries_filter_and_recent_updates():
    """分类/标签过滤 + 最近更新排序。"""
    d = tempfile.mkdtemp()
    p = Path(d) / "wb.json"
    _write_book(p, _sample_entries())
    wb = WorldBook(p)
    assert len(wb.get_entries(category="character")) == 3
    assert len(wb.get_entries(tag="话题")) == 1
    assert len(wb.get_entries()) == 5
    assert wb.get_recent_updates(limit=2)  # 非空即可（时间戳排序）


# =====================================================================
# 自动进化（弹幕/礼物事件订阅）
# =====================================================================

def _make_evolving_book(thresholds=None):
    from src.shared.event_bus import EventBus
    d = tempfile.mkdtemp()
    p = Path(d) / "wb.json"
    _write_book(p, [])
    bus = EventBus()
    bus.reset()
    cfg = {"topic_threshold": 2, "viewer_gift_value": 100,
           "viewer_msg_count": 3}
    if thresholds:
        cfg.update(thresholds)
    wb = WorldBook(p, evolve_cfg=cfg)
    wb.start_evolving(bus)
    return wb, bus, p


def test_evolve_topic_from_danmaku():
    """弹幕话题频率 ≥ 阈值 → 生成观众话题条目（audience_insight）。"""
    wb, bus, p = _make_evolving_book()
    for _ in range(3):
        bus.publish("danmaku:received", content="哈哈哈哈哈", user_name="观众A",
                    user_id="uid-a")
    topics = wb.get_entries_by_category("audience_insight")
    assert topics, "话题达到阈值应生成条目"
    assert topics[0]["title"] == "观众话题：哈哈"
    assert all(e["tags"][0] == "话题" for e in topics)


def test_evolve_important_viewer_from_gift():
    """礼物价值（金瓜子=price×num）≥ 阈值 → 重要观众条目。"""
    wb, bus, p = _make_evolving_book()
    bus.publish("gift:received", user_name="土豪哥",
                extra={"num": 2, "price": 100, "gift_name": "辣条"})
    viewers = [e for e in wb.get_entries_by_category("viewer")
               if e["tags"][0] == "重要观众"]
    assert viewers and "土豪哥" in viewers[0]["title"]


def test_evolve_regular_viewer_from_msg_count():
    """发言数 ≥ 阈值 → 常驻观众条目。"""
    wb, bus, p = _make_evolving_book()
    for _ in range(4):
        bus.publish("danmaku:received", content="晚安", user_name="夜猫子",
                    user_id="uid-night")
    viewers = [e for e in wb.get_entries_by_category("viewer")
               if e["tags"][0] == "常驻观众"]
    assert viewers and "夜猫子" in viewers[0]["title"]


def test_evolve_disabled_switch():
    """enabled=False：不生成条目。"""
    wb, bus, p = _make_evolving_book(thresholds={"enabled": False})
    for _ in range(5):
        bus.publish("danmaku:received", content="哈哈哈哈哈", user_name="观众A")
    assert wb.get_entries_by_category("audience_insight") == []


def test_start_evolving_idempotent_and_stop():
    """start 幂等；stop 退订后不再进化。"""
    from src.shared.event_bus import EventBus
    d = tempfile.mkdtemp()
    p = Path(d) / "wb.json"
    _write_book(p, [])
    bus = EventBus()
    bus.reset()
    wb = WorldBook(p, evolve_cfg={"topic_threshold": 1})
    wb.start_evolving(bus)
    wb.start_evolving(bus)  # 幂等
    assert wb.stats()["evolving"] is True
    wb.stop_evolving()
    assert wb.stats()["evolving"] is False
    for _ in range(3):
        bus.publish("danmaku:received", content="测试话题词", user_name="A")
    assert wb.get_entries_by_category("audience_insight") == []


# =====================================================================
# 增强：建议 / 手动进化 / 合并
# =====================================================================

def test_suggest_new_topic_and_character():
    """suggest：新话题/新角色 → high 优先级建议。"""
    d = tempfile.mkdtemp()
    p = Path(d) / "wb.json"
    _write_book(p, _sample_entries())
    wb = WorldBook(p)
    sugg = wb.suggest({"topics": ["新梗"], "characters": ["新人角色"]})
    assert sugg and sugg[0]["priority"] == "high"
    assert any(s["type"] == "new_entry" for s in sugg)


def test_evolve_manual_updates_entry():
    """手动进化：事件文本命中条目标题 → 更新该条目。"""
    d = tempfile.mkdtemp()
    p = Path(d) / "wb.json"
    _write_book(p, _sample_entries())
    wb = WorldBook(p)
    modified = wb.evolve({"type": "story", "text": "Yuki 的身份 有更新"})
    assert "wb_yuki_identity" in modified
    assert wb.get_entry("wb_yuki_identity")["metadata"]["last_event"] == "story"


def test_merge_entries():
    """合并：两个条目 → 一个新条目，原条目删除。"""
    d = tempfile.mkdtemp()
    p = Path(d) / "wb.json"
    _write_book(p, _sample_entries())
    wb = WorldBook(p)
    new_id = wb.merge_entries("wb_yuki_identity", "wb_yuki_speaking")
    assert new_id and wb.get_entry(new_id) is not None
    assert wb.get_entry("wb_yuki_identity") is None
    assert wb.get_entry("wb_yuki_speaking") is None
    assert wb.merge_entries("wb_yuki_identity", "wb_lilith_identity") is None  # 已删除
