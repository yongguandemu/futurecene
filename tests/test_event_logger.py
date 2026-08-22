"""test_event_logger.py — 事件记录器单测（L0 JSONL + L1 缓冲，任务四）"""
import json
import time

from src.orchestrators.memory_orchestrator.event_logger import EventLogger
from src.shared.event_bus import EventBus
from src.shared.events import (
    DANMAKU_RECEIVED,
    LIVE2D_PARAMS_BATCH,
    LLM_RESPONDED,
    MEMORY_EVENT_LOGGED,
)


def _make(tmp_path):
    bus = EventBus()
    bus.reset()
    return EventLogger(bus, l0_dir=str(tmp_path / "l0")), bus


def test_l0_jsonl_write(tmp_path):
    logger_inst, bus = _make(tmp_path)
    logger_inst.start()
    bus.publish(DANMAKU_RECEIVED, content="主播好厉害")
    assert logger_inst.flush() == 1
    files = list((tmp_path / "l0").glob("*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["event"] == DANMAKU_RECEIVED
    assert rows[0]["data"]["content"] == "主播好厉害"
    logger_inst.stop()


def test_l1_buffer_and_text(tmp_path):
    logger_inst, bus = _make(tmp_path)
    logger_inst.start()
    bus.publish(DANMAKU_RECEIVED, content="主播好厉害")
    bus.publish(LLM_RESPONDED, capability="llm:chat", text="感谢大家的喜欢")
    entries = logger_inst.l1_entries()
    assert len(entries) == 2
    contents = [e["content"] for e in entries]
    assert "观众弹幕：主播好厉害" in contents
    assert "我说：感谢大家的喜欢" in contents
    assert all(e["event"] in (DANMAKU_RECEIVED, LLM_RESPONDED) for e in entries)
    logger_inst.stop()


def test_high_freq_event_excluded_from_l1_but_l0_kept(tmp_path):
    logger_inst, bus = _make(tmp_path)
    logger_inst.start()
    bus.publish(LIVE2D_PARAMS_BATCH, role="yuki", params={"param": 1.0})
    assert logger_inst.count_l1() == 0
    assert logger_inst.flush() == 1  # L0 仍全量记录
    logger_inst.stop()


def test_no_recursion_on_own_event(tmp_path):
    """自身事件不重发不写 L0（防递归）。"""
    logger_inst, bus = _make(tmp_path)
    logger_inst.start()
    bus.publish(DANMAKU_RECEIVED, content="hi")
    logger_inst.flush()
    files = list((tmp_path / "l0").glob("*.jsonl"))
    rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    events_in_file = [r["event"] for r in rows]
    assert events_in_file == [DANMAKU_RECEIVED]
    assert MEMORY_EVENT_LOGGED not in events_in_file
    logger_inst.stop()


def test_l1_window_eviction(tmp_path):
    logger_inst, bus = _make(tmp_path)
    logger_inst.start()
    bus.publish(DANMAKU_RECEIVED, content="旧消息")
    assert logger_inst.count_l1() == 1
    # 把条目时间戳拨到时间窗外（窗口默认 600s）
    with logger_inst._lock:
        for entry in logger_inst._l1:
            entry["timestamp"] = time.time() - 3600
    assert logger_inst.count_l1() == 0
    logger_inst.stop()


def test_l1_max_entries_cap(tmp_path):
    from src.orchestrators.memory_orchestrator.memory_config import MemoryConfig

    class _Cfg(MemoryConfig):
        def __init__(self):
            self.l1_max_entries = 3
            self.l1_window_sec = 600.0
            self.l1_max_content_chars = 300
            self.l0_retention_days = 14

    bus = EventBus()
    bus.reset()
    logger_inst = EventLogger(bus, config=_Cfg(), l0_dir=str(tmp_path / "l0"))
    logger_inst.start()
    for i in range(5):
        bus.publish(DANMAKU_RECEIVED, content=f"弹幕{i}")
    assert logger_inst.count_l1() == 3  # 循环缓冲容量上限
    logger_inst.stop()


def test_cleanup_old_files(tmp_path):
    from src.orchestrators.memory_orchestrator.memory_config import MemoryConfig

    class _Cfg(MemoryConfig):
        def __init__(self):
            self.l0_retention_days = 14
            self.l1_window_sec = 600.0
            self.l1_max_entries = 500
            self.l1_max_content_chars = 300

    l0 = tmp_path / "l0"
    l0.mkdir(parents=True, exist_ok=True)
    (l0 / "20200101.jsonl").write_text("{}\n", encoding="utf-8")  # 超期
    today_name = time.strftime("%Y%m%d")
    (l0 / f"{today_name}.jsonl").write_text("{}\n", encoding="utf-8")  # 当日
    bus = EventBus()
    bus.reset()
    logger_inst = EventLogger(bus, config=_Cfg(), l0_dir=str(l0))
    logger_inst.start()  # 启动即清理
    assert not (l0 / "20200101.jsonl").exists()
    assert (l0 / f"{today_name}.jsonl").exists()
    logger_inst.stop()
