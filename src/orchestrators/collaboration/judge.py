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


import json  # noqa: E402
import re  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from src.orchestrators.collaboration.rules import make_rules_by_order  # noqa: E402

DEFAULT_RULES_ORDER = ["mention", "intent", "continuation", "relevance",
                       "balance", "cooldown", "random"]
_JUDGE_SYSTEM = (
    "你是直播间双虚拟主播（Yuki/Lilith）的发言权判断器。根据弹幕内容与最近话轮，"
    "判断谁更应当说话（或都不说话）。\n"
    "判断依据：\n"
    "- 弹幕点名或明显在与某角色互动 → 该角色紧迫度更高；\n"
    "- 弹幕内容贴近某角色人设/擅长话题 → 该角色优先；\n"
    "- 最近话轮中某角色刚说过话 → 适当降低其紧迫度（避免抢话）；\n"
    "- 弹幕与角色无关、无需回应（如纯表情刷屏）→ silent=true，双紧迫度均低；\n"
    "- 紧迫度取值 0-1：0=完全无需说话，1=必须立即回应。\n"
    "只输出 JSON，不要其它文字，格式：\n"
    '{"yuki": 0到1的紧迫度, "lilith": 0到1的紧迫度, "silent": true或false}\n'
    "示例（弹幕点名 Yuki）：{\"yuki\": 0.8, \"lilith\": 0.1, \"silent\": false}；"
    "示例（纯表情刷屏）：{\"yuki\": 0.0, \"lilith\": 0.0, \"silent\": true}"
)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class LLMJudge:
    """轻量 LLM 判断：注入弹幕 + 最近话轮 + 角色画像 → 结构化紧迫度。

    预算：budget_per_min 次/分钟（成功调用计数），超预算回退 RulesJudge。
    失败：LLM 异常 / JSON 解析失败 → 回退 RulesJudge（source=rules-fallback）。
    """

    def __init__(self, llm, profiles, budget_per_min: int = 4,
                 rules_order: List[str] = None,
                 rng_seed: int = None):
        self._llm = llm
        self._profiles = profiles
        self._budget = max(1, int(budget_per_min))
        self._calls: List[float] = []
        self._lock = threading.Lock()
        self._fallback = RulesJudge(
            make_rules_by_order(rules_order or DEFAULT_RULES_ORDER, seed=rng_seed))

    def _budget_available(self) -> bool:
        now = time.time()
        with self._lock:
            self._calls = [t for t in self._calls if now - t < 60.0]
            return len(self._calls) < self._budget

    def _fallback_result(self, note: str, ctx) -> UrgencyResult:
        r = self._fallback.judge(ctx)
        return UrgencyResult(r.urgencies, r.silent, f"fallback:{note}", "rules-fallback")

    def _build_prompt(self, ctx: ArbitrationContext) -> str:
        turns = []
        history_fn = getattr(ctx.turn_tracker, "turn_history", None)
        if callable(history_fn):
            for t in (history_fn(limit=5) or []):
                if t.get("role") and t.get("text"):
                    turns.append(f"{t['role']}: {t['text'][:60]}")
        personas = {}
        for role in sorted(ctx.present_roles):
            try:
                p = self._profiles.load(role)
                personas[role] = (getattr(p, "system_prompt", "") or "")[:120]
            except Exception:
                personas[role] = ""
        return (
            f"弹幕：{ctx.text}\n"
            f"最近对话：\n" + ("\n".join(turns[-3:]) or "（无）") + "\n"
            f"角色画像：\n" + "\n".join(f"{r}: {personas[r] or '（无）'}"
                                        for r in sorted(ctx.present_roles)) + "\n"
            f"在场：{sorted(ctx.present_roles)}"
        )

    def _parse(self, reply: str):
        m = _JSON_RE.search(reply or "")
        if not m:
            raise ValueError("reply 无 JSON")
        data = json.loads(m.group(0))
        urgencies = {}
        for role in (data or {}):
            if role == "silent":
                continue
            try:
                urgencies[role] = max(0.0, min(1.0, float(data[role])))
            except (TypeError, ValueError):
                continue
        silent = bool((data or {}).get("silent", False))
        return urgencies, silent

    def judge(self, ctx: ArbitrationContext) -> UrgencyResult:
        if not self._budget_available():
            return self._fallback_result("budget", ctx)
        try:
            resp = self._llm._chat({
                "text": self._build_prompt(ctx),
                "system_prompt": _JUDGE_SYSTEM,
                "history": [],
            })
            reply = ((resp or {}).get("data") or {}).get("reply", "") or ""
            urgencies, silent = self._parse(reply)
        except Exception as exc:
            return self._fallback_result(f"llm-error:{type(exc).__name__}", ctx)
        with self._lock:
            self._calls.append(time.time())
        return UrgencyResult(urgencies, silent, "llm", "llm")
