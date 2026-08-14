"""test_decision_policy.py — 决策分级与上抛策略（规格书 5.6）

覆盖：
1. 硬规则命中 → L0
2. 决策归属矩阵显式声明（精确 + 通配前缀）
3. 三问推断（域内 / 跨域 / 全局上下文 / 不可逆 / 高风险）
4. 用户验收场景：游戏操作/切歌/截图 → L1；发言权 → L2；开播下播/回复弹幕 → L3；脏话 → L0
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.shared.decision_policy import (
    DECISION_MATRIX, DecisionRequest, RiskLevel, classify, classify_capability,
    matrix_lookup,
)


def test_hard_rule_hits_l0():
    v = classify_capability("danmaku:any", has_hard_rule=True,
                            rule_hit="keyword_filter:脏话")
    assert v.layer == "L0"
    assert v.outcome == "block"
    assert v.matched_rule == "keyword_filter:脏话"
    assert v.source == "hard_rule"


# ---------- 决策归属矩阵：精确键 ----------

def test_matrix_l0_exact():
    assert classify_capability("safety:check_input").layer == "L0"
    assert classify_capability("safety:check_output").outcome == "block"


def test_matrix_l1_exact():
    assert classify_capability("music:next").layer == "L1"
    assert classify_capability("screen:capture").layer == "L1"
    assert classify_capability("bilibili:send_message").layer == "L1"


def test_matrix_l2_exact():
    assert classify_capability("collab:arbitrate").layer == "L2"
    assert classify_capability("game:mc_start").layer == "L2"
    assert classify_capability("game:vn_stop").layer == "L2"


def test_matrix_l3_exact():
    assert classify_capability("stream:start").layer == "L3"
    assert classify_capability("stream:stop").layer == "L3"
    assert classify_capability("llm:chat").layer == "L3"
    assert classify_capability("adapter:obs_stream").layer == "L3"
    assert classify_capability("session:switch").layer == "L3"
    assert classify_capability("game:commentary").layer == "L3"


# ---------- 决策归属矩阵：通配前缀 ----------

def test_matrix_wildcard_prefix():
    assert classify_capability("music:volume").layer == "L1"      # music:*
    assert classify_capability("experience:decide").layer == "L1"  # experience:*
    assert classify_capability("screen:keypress").layer == "L1"    # screen:*
    assert classify_capability("adapter:vts_param").layer == "L1"  # adapter:vts_*
    assert classify_capability("adapter:qq_send_group").layer == "L1"  # adapter:qq_send_*
    assert classify_capability("stream:app_terminate").layer == "L1"   # stream:app_*
    assert classify_capability("llm:stream_chunk").layer == "L3"  # llm:*


def test_matrix_exact_beats_wildcard():
    # screen:capture 有精确键，命中精确而非 screen:*
    assert matrix_lookup("screen:capture") is DECISION_MATRIX["screen:capture"]
    # llm:chat 精确键优先于 llm:*
    assert matrix_lookup("llm:chat") is DECISION_MATRIX["llm:chat"]


# ---------- 三问推断（矩阵未登记的能力） ----------

def test_infer_local_safe_to_l1():
    v = classify(DecisionRequest.of("future:play", "future",
                                    affected_domains=(), reversible=True,
                                    risk=RiskLevel.LOW,
                                    needs_global_context=False))
    assert v.layer == "L1"
    assert v.outcome == "execute"
    assert v.source == "infer"


def test_infer_cross_domain_to_l2():
    v = classify(DecisionRequest.of("future:act", "future",
                                    affected_domains=("tts", "live2d"),
                                    reversible=True, risk=RiskLevel.LOW))
    assert v.layer == "L2"
    assert v.outcome == "escalate"


def test_infer_global_context_to_l3():
    v = classify(DecisionRequest.of("future:act", "future",
                                    needs_global_context=True))
    assert v.layer == "L3"


def test_infer_irreversible_to_l3():
    v = classify(DecisionRequest.of("future:act", "future", reversible=False))
    assert v.layer == "L3"


def test_infer_high_risk_to_l3():
    v = classify(DecisionRequest.of("future:act", "future", risk=RiskLevel.HIGH))
    assert v.layer == "L3"


def test_unknown_capability_falls_back_l1():
    v = classify_capability("unknown:something")
    assert v.layer == "L1"
    assert v.source == "infer"


# ---------- 用户验收场景（规格书 5.6.3 归属矩阵关键行） ----------

def test_user_named_cases():
    cases = {
        # 域内自治 L1：游戏操作 / 切歌 / 截图
        "experience:decide": "L1",
        "music:next": "L1",
        "screen:capture": "L1",
        # 仲裁上抛 L2：发言权
        "collab:arbitrate": "L2",
        # 总脑编排 L3：开播 / 下播 / 回复弹幕
        "stream:start": "L3",
        "stream:stop": "L3",
        "llm:chat": "L3",
        # 反射 L0：脏话拦截
        "safety:check_input": "L0",
    }
    for capability, expected in cases.items():
        assert classify_capability(capability).layer == expected, capability
