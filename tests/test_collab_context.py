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
    prompt = cm.build_system_prompt("yuki", partner_lines="你的搭档Lilith刚才说：...")
    assert "你的搭档Lilith" in prompt
