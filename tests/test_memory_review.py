"""test_memory_review.py — 世界书提案审批流单测（任务四）"""
from src.orchestrators.memory_orchestrator.review import MemoryReview


def _make(tmp_path):
    return MemoryReview(data_file=str(tmp_path / "review.json"),
                        switch_check=lambda name: name == "allow_memory_to_worldbook")


def test_propose_when_switch_on(tmp_path):
    review = _make(tmp_path)
    pid = review.propose("l1", "Yuki 喜欢推理题材", "高频话题")
    assert pid == "proposal1"
    assert review.count("pending") == 1
    proposal = review.get(pid)
    assert proposal["source_memory_id"] == "l1"
    assert proposal["status"] == "pending"


def test_propose_when_switch_off(tmp_path):
    review = MemoryReview(data_file=str(tmp_path / "r.json"),
                          switch_check=lambda name: False)
    assert review.propose("l1", "内容") is None
    assert review.count() == 0


def test_propose_empty_content_rejected(tmp_path):
    review = _make(tmp_path)
    assert review.propose("l1", "   ") is None
    assert review.count() == 0


def test_propose_dedup_same_source_and_content(tmp_path):
    review = _make(tmp_path)
    pid1 = review.propose("l1", "内容A", "原因")
    pid2 = review.propose("l1", "内容A", "原因")
    assert pid1 == pid2
    assert review.count() == 1
    # 不同来源不合并
    pid3 = review.propose("l2", "内容A", "原因")
    assert pid3 != pid1
    assert review.count() == 2


def test_accept_and_reject(tmp_path):
    review = _make(tmp_path)
    pid = review.propose("l1", "内容")
    assert review.accept(pid) is True
    assert review.get(pid)["status"] == "accepted"
    assert review.accept(pid) is False  # 已处置不可重复
    pid2 = review.propose("l2", "另一条")
    assert review.reject(pid2, "不准确") is True
    assert review.get(pid2)["status"] == "rejected"
    assert review.get(pid2)["reason"] == "不准确"


def test_unknown_proposal_id(tmp_path):
    review = _make(tmp_path)
    assert review.accept("proposal999") is False
    assert review.reject("proposal999") is False
    assert review.get("proposal999") is None


def test_persistence_across_reload(tmp_path):
    review = _make(tmp_path)
    review.propose("l1", "持久化内容", "原因")
    review2 = MemoryReview(data_file=str(tmp_path / "review.json"),
                           switch_check=lambda name: True)
    assert review2.count("pending") == 1
    assert review2.get("proposal1")["proposed_content"] == "持久化内容"
    # 新实例继续新增提案，序号延续
    pid = review2.propose("l2", "第二条")
    assert pid == "proposal2"
    review3 = MemoryReview(data_file=str(tmp_path / "review.json"),
                           switch_check=lambda name: True)
    assert review3.count() == 2


def test_list_filter_by_status(tmp_path):
    review = _make(tmp_path)
    p1 = review.propose("l1", "内容一")
    review.accept(p1)
    review.propose("l2", "内容二")
    assert len(review.list(status="pending")) == 1
    assert len(review.list(status="accepted")) == 1
    assert review.count() == 2
