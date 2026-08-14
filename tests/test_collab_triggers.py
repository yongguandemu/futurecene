"""triggers 单测。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.collaboration.triggers import CollabTriggers


def test_banter_proposal_after_speech():
    tr = CollabTriggers(probability=1.0, global_cooldown=0.0,
                        present_roles={"yuki", "lilith"})
    props = tr.evaluate("yuki", "今天讲个故事吧")
    assert props and props[0]["role"] == "lilith" and props[0]["kind"] == "banter"


def test_global_cooldown_blocks():
    tr = CollabTriggers(probability=1.0, global_cooldown=3600.0,
                        present_roles={"yuki", "lilith"})
    tr.evaluate("yuki", "第一条")
    props = tr.evaluate("lilith", "第二条")   # 冷却期内
    assert props == []


def test_probability_zero_disables():
    tr = CollabTriggers(probability=0.0, global_cooldown=0.0,
                        present_roles={"yuki", "lilith"})
    assert tr.evaluate("yuki", "随便") == []
