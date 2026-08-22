"""test_memory_compressor.py — 压缩器单测（L1→L2→L3，任务四）

覆盖：阈值触发、摘要落 L2、模型不可用降级原文分段、L2→L3 归档、开关关闭不触发。
"""
import time

from src.orchestrators.memory_orchestrator.event_logger import EventLogger
from src.orchestrators.memory_orchestrator.long_term import LongTermMemory
from src.orchestrators.memory_orchestrator.memory_compressor import (
    MemoryCompressor,
    MidTermMemory,
)
from src.shared.event_bus import EventBus
from src.shared.events import DANMAKU_RECEIVED, MEMORY_EVENT_LOGGED


def _make(tmp_path):
    bus = EventBus()
    bus.reset()
    mid = MidTermMemory(db_path=str(tmp_path / "l2.db"))
    long_mem = LongTermMemory(db_path=str(tmp_path / "l3.db"))
    return bus, mid, long_mem


def _big_entries(n=5, repeat=400):
    """n 条 × 800 字 = 4000 字（超过 3000 阈值）。"""
    return [{"memory_id": f"e{i}", "content": "弹幕" * repeat} for i in range(1, n + 1)]


def test_compress_trigger_and_summary(tmp_path):
    bus, mid, long_mem = _make(tmp_path)
    compressor = MemoryCompressor(
        bus, mid, long_mem,
        summarize_fn=lambda text, max_chars: f"摘要：{text[:10]}…")
    ok = compressor.compress_now("default", entries=_big_entries())
    assert ok is True
    recent = mid.get_recent("default")
    assert len(recent) == 1
    assert "摘要" in recent[0]["summary"]
    assert compressor._last_seq == 5  # 增量游标推进
    mid.close()
    long_mem.close()


def test_compress_below_threshold_noop(tmp_path):
    bus, mid, long_mem = _make(tmp_path)
    compressor = MemoryCompressor(bus, mid, long_mem,
                                  summarize_fn=lambda text, max_chars: "摘要")
    ok = compressor.compress_now("default", entries=[{"memory_id": "e1", "content": "短"}])
    assert ok is False
    assert mid.count("default") == 0
    mid.close()
    long_mem.close()


def test_compress_fallback_when_no_model(tmp_path):
    """模型不可用（无 summarize_fn）→ 原文分段落 L2（规格书降级路径）。"""
    bus, mid, long_mem = _make(tmp_path)
    compressor = MemoryCompressor(bus, mid, long_mem)
    ok = compressor.compress_now("default", entries=_big_entries())
    assert ok is True
    recent = mid.get_recent("default")
    assert recent  # 分段落库
    assert all(len(e["summary"]) <= 800 for e in recent)  # 每段不超上限
    mid.close()
    long_mem.close()


def test_consolidate_to_l3(tmp_path):
    bus, mid, long_mem = _make(tmp_path)
    compressor = MemoryCompressor(bus, mid, long_mem,
                                  summarize_fn=lambda text, max_chars: text[:max_chars])
    mid.store("default", "中期摘要" * 200, ["e1"])
    n = compressor.consolidate_to_l3("default")
    assert n == 1
    assert long_mem.count() == 1
    assert mid.get_recent("default") == []  # 已归档，默认检索不再返回
    assert compressor.consolidate_to_l3("default") == 0  # 不重复归档
    mid.close()
    long_mem.close()


def test_event_driven_compression(tmp_path):
    """L0 落盘事件驱动：累计达阈值 → 异步压缩。"""
    bus, mid, long_mem = _make(tmp_path)
    logger_inst = EventLogger(bus, l0_dir=str(tmp_path / "l0"), batch_size=100)
    compressor = MemoryCompressor(
        bus, mid, long_mem,
        summarize_fn=lambda text, max_chars: "事件驱动摘要",
        l1_entries_fn=logger_inst.l1_entries)
    logger_inst.start()
    compressor.start()
    for _ in range(10):
        bus.publish(DANMAKU_RECEIVED, content="长弹幕内容" * 100)  # 500 字/条
    deadline = time.time() + 3.0
    while mid.count("default") == 0 and time.time() < deadline:
        time.sleep(0.05)
    assert mid.count("default") >= 1
    assert compressor._last_seq >= 10
    compressor.stop()
    logger_inst.stop()
    mid.close()
    long_mem.close()


def test_event_driven_switch_off_no_trigger(tmp_path):
    """memory_compression 开关关 → 事件驱动不触发压缩。"""
    bus, mid, long_mem = _make(tmp_path)
    compressor = MemoryCompressor(bus, mid, long_mem,
                                  switch_check=lambda name: False)
    compressor.start()
    before = len(compressor._threads)
    bus.publish(MEMORY_EVENT_LOGGED, source_event="x", ts=0.0)
    assert len(compressor._threads) == before
    assert mid.count("default") == 0
    compressor.stop()
    mid.close()
    long_mem.close()


def test_mid_term_memory_roundtrip(tmp_path):
    bus, mid, long_mem = _make(tmp_path)
    mid_id = mid.store("default", "一段摘要", ["e1", "e2"])
    assert mid_id.startswith("m2")
    recent = mid.get_recent("default")
    assert recent[0]["summary"] == "一段摘要"
    assert recent[0]["source_ids"] == ["e1", "e2"]
    assert mid.count("default") == 1
    mid.close()
    long_mem.close()
