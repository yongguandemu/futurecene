"""test_app_role_topic.py — 主动对话角色化话题生成（世界书注入）测试

验证 _role_topic 生成的 system_prompt 同时携带角色人设与世界书设定块，
防止主动对话冷场闲聊脱离世界观（此前仅传 persona，未追加世界书）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

from unittest import mock

from src.app import _role_topic


class FakeProfile:
    system_prompt = "你是yuki，一个刚被唤醒的AI实习生。"
    behavior_rules = {"rules": {"preferred_topics": ["AI实习"], "avoid_topics": ["恐怖"]}}


class FakeLoader:
    def load(self, role):
        return FakeProfile()


class FakeSession:
    present_roles = ["yuki", "lilith"]


class FakeCollab:
    def snapshot(self):
        return {"recent_turns": []}


class FakeLLM:
    def __init__(self):
        self.system_prompt = ""

    def _chat(self, payload):
        self.system_prompt = payload.get("system_prompt", "")
        return {"data": {"reply": "测试话题"}}


def test_role_topic_appends_world_book_block():
    llm = FakeLLM()
    fake_wb = mock.Mock()
    fake_wb.system_prompt_block.return_value = "【世界设定】这里是未来都市，AI与人类共存。"
    with mock.patch("src.shared.world_book.get_world_book", return_value=fake_wb):
        result = _role_topic("yuki", FakeLoader(), FakeSession(), FakeCollab(), llm)
    assert result["text"] == "测试话题"
    sp = llm.system_prompt
    # 人设 + 世界书块都注入
    assert "刚被唤醒的AI实习生" in sp
    assert "这里是未来都市" in sp
    fake_wb.system_prompt_block.assert_called_once_with("yuki")


def test_role_topic_without_world_book_is_backward_compatible():
    llm = FakeLLM()
    fake_wb = mock.Mock()
    fake_wb.system_prompt_block.return_value = ""
    with mock.patch("src.shared.world_book.get_world_book", return_value=fake_wb):
        result = _role_topic("yuki", FakeLoader(), FakeSession(), FakeCollab(), llm)
    assert result["text"] == "测试话题"
    # 世界书为空时仅人设，不报错
    assert "刚被唤醒的AI实习生" in llm.system_prompt


def test_role_topic_falls_back_to_topic_pool_on_llm_failure():
    class BoomLLM:
        def _chat(self, payload):
            raise RuntimeError("llm down")

    fake_wb = mock.Mock()
    fake_wb.system_prompt_block.return_value = ""
    with mock.patch("src.shared.world_book.get_world_book", return_value=fake_wb):
        result = _role_topic("yuki", FakeLoader(), FakeSession(), FakeCollab(), BoomLLM())
    # 回退到话题池，仍有文本
    assert result and result["text"]