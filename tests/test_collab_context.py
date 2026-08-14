"""context_manager 单测。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.collaboration.context_manager import ContextManager


def test_memory_key_buckets_by_character():
    cm = ContextManager()
    assert cm.memory_key("yuki") == "default:yuki"
    assert cm.memory_key("lilith") != cm.memory_key("yuki")


def test_global_transcript_ring():
    cm = ContextManager(max_transcript=3)
    cm.record_turn("yuki", "hello yuki")
    cm.record_turn("lilith", "hello lilith")
    cm.record_turn("yuki", "third")
    cm.record_turn("lilith", "fourth")
    lines = cm.global_transcript()
    assert len(lines) == 3
    assert "fourth" in lines[-1] and "hello yuki" not in lines


def test_system_prompt_with_awareness():
    cm = ContextManager(max_partner_lines=2)
    cm.record_turn("lilith", "你刚才讲的故事不错")
    prompt = cm.build_system_prompt("yuki", base_prompt="你是Yuki")
    assert "【感知彼此】" in prompt
    assert "你刚才讲的故事不错" in prompt


def test_partner_lines_filters_self():
    cm = ContextManager(max_partner_lines=10)
    cm.record_turn("yuki", "今天天气不错")
    cm.record_turn("lilith", "是呀，适合出门")
    cm.record_turn("yuki", "那我们走吧")
    lines = cm.partner_lines("yuki")
    assert lines
    assert all(not ln.startswith("yuki:") for ln in lines)
    assert "是呀，适合出门" in "\n".join(lines)
