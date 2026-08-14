"""judge.py — 紧迫度判断器（V3：LLM 提议、机制裁决的单决策通道）

judge 协议：输入仲裁上下文，输出各角色紧迫度 + 是否建议沉默 + 来源。
- RulesJudge：规则链映射（默认，零 LLM，行为与 V2 仲裁完全一致）。
- LLMJudge：轻量 LLM 判断（预算控制 + 失败回退 RulesJudge），Task 7 实现。

护栏不在此层：互斥/冷却/排队由 arbitrator 在 judge 之后统一执行。

# 模块内容清单 — judge

## 1. 模块身份标识
- 所属调度官：collaboration（多角色协作域）
- 能力名：collab:arbitrate 的判断器来源（规则链 / LLM 紧迫度）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| rules（RulesJudge） | 是 | - | List[Rule] | 规则链（由 arbitrator 传入自身 _rules） |
| llm / profiles / budget_per_min（LLMJudge） | 是 | 4 次/分钟 | - | 轻量 LLM 判断 + 预算控制 + 失败回退 |

## 3. 输入契约
- 输入格式：`judge(ctx: ArbitrationContext) -> UrgencyResult`
- ctx：ArbitrationContext（text/present_roles/profiles/turn_tracker 等，与规则链共用）

## 4. 输出契约
- 成功：UrgencyResult(urgencies, silent, reason, source)；urgencies 为空 dict 表示无角色回应
- 失败：LLMJudge 异常/坏 JSON → 回退 RulesJudge（source=rules-fallback，reason=fallback:*）
- 事件：无（judge 结果由 arbitrator 汇总发布 collab:judge）

## 5. 依赖声明
- 外部服务：LLMJudge 依赖 LLM（llm._chat，结构化 JSON 输出）
- 内部模块：collaboration.rules（ArbitrationContext/Rule/RuleVerdict/make_rules_by_order）
- 预先配置：无

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| LLM 调用异常 | 网络/模型错误 | LLMJudge 捕获 → 回退 RulesJudge |
| JSON 解析失败 | LLM 输出非 JSON | 同上（reason=fallback:llm-error:*） |
| 预算耗尽 | 每分钟调用超 budget_per_min | 回退 RulesJudge（reason=fallback:budget） |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| judge(ctx) | 是 | 每次仲裁调用一次；LLMJudge 内部管理调用时间窗 |

## 8. 领域状态说明
- 状态项：RulesJudge 无状态；LLMJudge 持有 _calls（近 60s 调用时间戳，预算窗口）
- 持久化：无
- 恢复：无（每次构造全新实例）
"""
from dataclasses import dataclass
from typing import Dict, List, Protocol

from src.orchestrators.collaboration.rules import (
    ArbitrationContext,
    Rule,
    RuleVerdict,
)


@dataclass
class UrgencyResult:
    urgencies: Dict[str, float]   # role -> 0..1（空 dict 表示无角色回应）
    silent: bool                  # 是否建议沉默
    reason: str                   # 审计：judge 依据（mention:lilith / llm / fallback:*）
    source: str = "rules"         # rules | llm | rules-fallback


class UrgencyJudge(Protocol):
    """紧迫度判断器协议：输入仲裁上下文，输出各角色紧迫度。"""

    def judge(self, ctx: ArbitrationContext) -> UrgencyResult: ...


class RulesJudge:
    """规则链 → 紧迫度：按既有链序取首个非 None 胜者 → urgency=confidence，
    其余角色 0；链尾无命中（含空在场）→ silent=True。

    行为与 V2 仲裁（规则链首个非 None 即胜出）完全一致，作为默认判断器。
    """

    def __init__(self, rules: List[Rule]):
        self._rules = list(rules)

    def judge(self, ctx: ArbitrationContext) -> UrgencyResult:
        for rule in self._rules:
            verdict: RuleVerdict = rule.evaluate(ctx)
            if verdict.role is not None:
                return UrgencyResult({verdict.role: verdict.confidence}, False,
                                     verdict.reason, "rules")
        return UrgencyResult({}, True, "no-rule-hit", "rules")
