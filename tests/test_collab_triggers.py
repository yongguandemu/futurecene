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


def test_empty_present_roles_yields_no_proposal():
    """空集就是空集：构造时空集 / update_runtime 空集 / None 不更新，均无候选产出。"""
    tr = CollabTriggers(probability=1.0, global_cooldown=0.0,
                        present_roles=set(), seed=0)
    assert tr.evaluate("yuki", "hello") == []
    # update_runtime 显式空集：清空在场名单
    tr.update_runtime(probability=1.0, global_cooldown=0.0, present_roles=set())
    assert tr.evaluate("lilith", "hi") == []
    # update_runtime None：保留当前（空）名单，不回退默认值
    tr.update_runtime(probability=1.0, global_cooldown=0.0, present_roles=None)
    assert tr.evaluate("mio", "hey") == []


def test_default_present_roles_when_not_provided():
    """构造时未传 present_roles（None）才使用默认双人组。"""
    tr = CollabTriggers(probability=1.0, global_cooldown=0.0, seed=0)
    props = tr.evaluate("yuki", "故事")
    assert props and props[0]["role"] == "lilith"


def test_probability_half_seed_zero_hit_and_miss():
    """seed=0 固定断言：首抽未命中、第三抽命中，且两随机分支均被真实执行。"""
    tr = CollabTriggers(probability=0.5, global_cooldown=0.0,
                        present_roles={"yuki", "lilith"}, seed=0)
    results = [bool(tr.evaluate("yuki", f"t{i}")) for i in range(20)]
    assert results[0] is False    # seed=0 首抽未命中（固定断言）
    assert results[2] is True     # seed=0 第三抽命中（固定断言）
    assert True in results and False in results   # 命中/未命中分支都真实覆盖
    # 确定性：同 seed 同调用序列结果可复现
    tr2 = CollabTriggers(probability=0.5, global_cooldown=0.0,
                         present_roles={"yuki", "lilith"}, seed=0)
    results2 = [bool(tr2.evaluate("yuki", f"t{i}")) for i in range(20)]
    assert results == results2


def test_cooldown_not_consumed_on_probability_miss():
    """冷却为成功产出后的静默期：概率未命中不消耗冷却额度。"""
    tr = CollabTriggers(probability=0.5, global_cooldown=1000.0,
                        present_roles={"yuki", "lilith"}, seed=0)
    assert tr.evaluate("yuki", "miss1") == []   # 未命中：不消耗冷却
    assert tr.evaluate("yuki", "miss2") == []   # 未命中：不消耗冷却
    assert tr.evaluate("yuki", "hit")           # 命中：成功产出才进入冷却
    assert tr.evaluate("lilith", "blocked") == []  # 命中后冷却期内被阻断


def test_update_runtime_changes_behavior():
    """update_runtime 对 probability/cooldown/present_roles 的实时生效。"""
    tr = CollabTriggers(probability=0.0, global_cooldown=0.0,
                        present_roles={"yuki", "lilith"}, seed=0)
    assert tr.evaluate("yuki", "a") == []       # 初始概率 0
    tr.update_runtime(probability=1.0, global_cooldown=0.0,
                      present_roles={"yuki", "lilith"})
    assert tr.evaluate("yuki", "b")             # 概率调高后命中
    tr.update_runtime(probability=1.0, global_cooldown=3600.0,
                      present_roles={"yuki", "lilith"})
    assert tr.evaluate("lilith", "c") == []     # 冷却调大后阻断
    tr.update_runtime(probability=1.0, global_cooldown=0.0,
                      present_roles={"yuki", "lilith"})
    assert tr.evaluate("lilith", "d")           # 冷却清零后恢复
    tr.update_runtime(probability=1.0, global_cooldown=0.0,
                      present_roles={"yuki"})
    assert tr.evaluate("yuki", "e") == []       # 名单收缩：无他人候选
    tr.update_runtime(probability=1.0, global_cooldown=0.0,
                      present_roles={"yuki", "lilith"})
    assert tr.evaluate("yuki", "f")             # 名单恢复后再次命中
    tr.update_runtime(probability=1.0, global_cooldown=0.0,
                      present_roles=None)
    assert tr.evaluate("yuki", "g")             # None：名单保持不变仍可命中


def test_target_randomization_reproducible():
    """目标随机化：同 seed 结果可复现，且非恒取字典序第一。"""
    roles = {"yuki", "lilith", "mio", "aoi"}

    def run():
        tr = CollabTriggers(probability=1.0, global_cooldown=0.0,
                            present_roles=roles, seed=42)
        return [tr.evaluate("yuki", f"t{i}")[0]["role"] for i in range(6)]

    seq1 = run()
    seq2 = run()
    assert seq1 == seq2                       # 同 seed 同结果（确定性）
    assert seq1 == ["aoi", "aoi", "mio", "mio", "aoi", "aoi"]  # 固定断言
    assert len(set(seq1)) > 1                 # 随机化生效，非恒取字典序第一
