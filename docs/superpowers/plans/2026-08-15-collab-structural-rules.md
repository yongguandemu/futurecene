# 多角色协作演进：结构规则层（V2）+ 紧迫度判断器（V3）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 治理规则链的"纯随机"缺陷——V2 引入对话结构规则（延续/防垄断）与接话结构增强，让"谁该说话"从概率判断升级为结构判断；V3 提供可插拔的 LLM 紧迫度判断器（提议-裁决单通道），保留全部确定性护栏。

**Architecture:** 保持集中式协调器 + 确定性护栏不变（调研结论：单裁决者 + LLM 提议是最成熟形态）。V2 在 `rules.py` 规则链中插入两条确定性结构规则（延续规则、防垄断规则），并在 `triggers.py` 增加结构驱动的接话概率增强（提问必须被接、故事/玩笑倾向被接）；V3 新增 `judge.py`（紧迫度协议），`arbitrator` 从"规则链"切换到"紧迫度取最大"（LLM 提议 → 机制裁决），带预算与失败回退。

**Tech Stack:** Python 3.10 + pytest（TDD）；零新依赖。

**调研依据**：多智能体轮转研究显示"下一位说话者"是可预测的结构信号（相邻对/对象识别），LLM 预测甚至超过人类；成熟形态为"LLM 提议 + 确定性机制裁决"（详见规格书演进备忘）。

---

## 文件结构

```
修改：
src/orchestrators/collaboration/rules.py        +ContinuationRule +BalanceRule +默认链顺序
src/orchestrators/collaboration/triggers.py      +结构驱动接话概率增强
config/config.yaml                              rules_order 更新 + V2 参数（balance_max_run 等）
tests/test_collab_rules.py                      +新规则测试 + FakeTT 补 turn_history
tests/test_collab_triggers.py                   +结构增强测试
tests/test_collab_arbitrator.py                 链顺序适配（若受影响）
tests/test_collab_coordinator.py                适配（若受影响）
docs/superpowers/plans/2026-08-15-collab-structural-rules.md  本文件

新增（V3，本期只出设计不实现）：
src/orchestrators/collaboration/judge.py        （V3：紧迫度协议 + RulesJudge + LLMJudge）
```

---

# V2 · 结构规则层（本期实现）

### Task 1: ContinuationRule（延续规则）

**Files:**
- Modify: `src/orchestrators/collaboration/rules.py`
- Modify: `tests/test_collab_rules.py`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_collab_rules.py`）

```python
class FakeTTWithHistory:
    """带话轮历史的 turn_tracker 桩。"""
    def __init__(self, history=None, last=None):
        self._history = history or []
        self.last = last or {}

    def idle_seconds(self, role):
        return self.last.get(role, 1.0)

    def turn_history(self, limit=10):
        return list(self._history[-limit:])


_RESPONSE_MARKERS = ("谢谢", "好听", "好看", "笑死", "哈哈", "666", "牛", "对", "确实", "嗯嗯", "然后呢", "再来一个", "真的假的")


def test_continuation_rule_follows_last_speaker():
    tt = FakeTTWithHistory(history=[{"role": "yuki", "kind": "speech",
                                     "text": "今天给大家讲一个月亮邮差的故事", "ts": 1.0}])
    ctx = ArbitrationContext(text="这个故事真好听，再来一个", user_name="观众", source="danmaku",
                             kind="danmaku", lead_role="yuki",
                             present_roles={"yuki", "lilith"},
                             profiles=FakeProfiles(), turn_tracker=tt)
    v = ContinuationRule().evaluate(ctx)
    assert v.role == "yuki" and v.reason == "continuation:yuki"


def test_continuation_rule_no_signal_returns_none():
    tt = FakeTTWithHistory(history=[{"role": "lilith", "kind": "speech",
                                     "text": "哼，今天直播人气不错", "ts": 1.0}])
    ctx = ArbitrationContext(text="今天天气不错", user_name="观众", source="danmaku",
                             kind="danmaku", lead_role="yuki",
                             present_roles={"yuki", "lilith"},
                             profiles=FakeProfiles(), turn_tracker=tt)
    v = ContinuationRule().evaluate(ctx)
    assert v.role is None


def test_continuation_rule_empty_history_safe():
    ctx = ArbitrationContext(text="嗯嗯", user_name="观众", source="danmaku",
                             kind="danmaku", lead_role="yuki",
                             present_roles={"yuki", "lilith"},
                             profiles=FakeProfiles(), turn_tracker=FakeTTWithHistory())
    v = ContinuationRule().evaluate(ctx)
    assert v.role is None
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_collab_rules.py::test_continuation_rule_follows_last_speaker -v`
Expected: FAIL（ContinuationRule 不存在）

- [ ] **Step 3: 实现**（`src/orchestrators/collaboration/rules.py`）

```python
_RESPONSE_MARKERS = ("谢谢", "好听", "好看", "笑死", "哈哈", "666", "牛",
                     "对", "确实", "嗯嗯", "然后呢", "再来一个", "真的假的")


class ContinuationRule(Rule):
    """延续规则：观众在回应上一位发言者（响应词或话题重叠）→ 由上一位发言者继续。

    对话结构信号（相邻对）的确定性实现：响应标记词或与上轮文本的关键词重叠。
    无话轮历史或历史缺失时安全返回 None（不误判）。
    """
    name = "continuation"

    def evaluate(self, ctx):
        history_fn = getattr(ctx.turn_tracker, "turn_history", None)
        if not callable(history_fn):
            return RuleVerdict(None, 0.0, "no-history")
        history = history_fn(limit=3) or []
        last = None
        for entry in reversed(history):
            if entry.get("text"):
                last = entry
                break
        if last is None or last.get("role") not in ctx.present_roles:
            return RuleVerdict(None, 0.0, "no-last-turn")
        text = ctx.text
        if any(marker in text for marker in _RESPONSE_MARKERS):
            return RuleVerdict(last["role"], 0.9, f"continuation:{last['role']}")
        last_text = last.get("text") or ""
        overlap = {w for w in text if len(w) >= 2 and w in last_text}
        if overlap:
            return RuleVerdict(last["role"], 0.7, f"continuation:{last['role']}")
        return RuleVerdict(None, 0.0, "no-continuation-signal")
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_collab_rules.py -v`
Expected: PASS（含既有 10 项 + 新增 3 项）

- [ ] **Step 5: 提交**

```bash
git add src/orchestrators/collaboration/rules.py tests/test_collab_rules.py
git commit -m "feat(collab): 延续规则（相邻对结构信号，观众回应上一位发言者）"
```

---

### Task 2: BalanceRule（防垄断规则）

**Files:**
- Modify: `src/orchestrators/collaboration/rules.py`
- Modify: `tests/test_collab_rules.py`

- [ ] **Step 1: 写失败测试**

```python
def test_balance_rule_prefers_other_after_monopoly():
    tt = FakeTTWithHistory(history=[
        {"role": "yuki", "kind": "speech", "text": "故事一", "ts": 1.0},
        {"role": "yuki", "kind": "speech", "text": "故事二", "ts": 2.0},
    ])
    ctx = ArbitrationContext(text="随便聊聊", user_name="观众", source="danmaku",
                             kind="danmaku", lead_role="yuki",
                             present_roles={"yuki", "lilith"},
                             profiles=FakeProfiles(), turn_tracker=tt)
    v = BalanceRule(max_run=2).evaluate(ctx)
    assert v.role == "lilith" and v.reason == "balance:lilith"


def test_balance_rule_not_fire_below_run_threshold():
    tt = FakeTTWithHistory(history=[{"role": "yuki", "kind": "speech", "text": "故事一", "ts": 1.0}])
    ctx = ArbitrationContext(text="随便聊聊", user_name="观众", source="danmaku",
                             kind="danmaku", lead_role="yuki",
                             present_roles={"yuki", "lilith"},
                             profiles=FakeProfiles(), turn_tracker=tt)
    assert BalanceRule(max_run=2).evaluate(ctx).role is None
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_collab_rules.py::test_balance_rule_prefers_other_after_monopoly -v`
Expected: FAIL（BalanceRule 不存在）

- [ ] **Step 3: 实现**（`src/orchestrators/collaboration/rules.py`）

```python
class BalanceRule(Rule):
    """防垄断规则：同一位角色连续发言超过 max_run 次且无更强信号时，换另一位角色。"""
    name = "balance"

    def __init__(self, max_run: int = 2):
        self._max_run = int(max_run)

    def evaluate(self, ctx):
        history_fn = getattr(ctx.turn_tracker, "turn_history", None)
        if not callable(history_fn):
            return RuleVerdict(None, 0.0, "no-history")
        history = history_fn(limit=self._max_run + 1) or []
        run = 0
        last_role = None
        for entry in reversed(history):
            if not entry.get("role"):
                continue
            if last_role is None:
                last_role = entry["role"]
                run = 1
            elif entry["role"] == last_role:
                run += 1
            else:
                break
        if run >= self._max_run:
            others = [r for r in sorted(ctx.present_roles) if r != last_role]
            if others:
                return RuleVerdict(others[0], 0.6, f"balance:{others[0]}")
        return RuleVerdict(None, 0.0, "no-monopoly")
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_collab_rules.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/orchestrators/collaboration/rules.py tests/test_collab_rules.py
git commit -m "feat(collab): 防垄断规则（连续发言超阈值换人）"
```

---

### Task 3: 默认链顺序 + 配置更新

**Files:**
- Modify: `src/orchestrators/collaboration/rules.py`（`build_default_rules` / `make_rules_by_order`）
- Modify: `config/config.yaml`（`collaboration.rules_order` + `balance.max_run`）
- Modify: `tests/test_collab_rules.py`（链顺序断言适配）

- [ ] **Step 1: 更新默认链**（`rules.py`）

```python
def build_default_rules(seed: Optional[int] = None, balance_max_run: int = 2) -> List[Rule]:
    return [MentionRule(), IntentRule(), ContinuationRule(),
            RelevanceRule(), BalanceRule(max_run=balance_max_run),
            CooldownRule(), RandomRule(seed=seed)]


def make_rules_by_order(names: List[str], seed: Optional[int] = None,
                        balance_max_run: int = 2) -> List[Rule]:
    pool = {r.name: r for r in build_default_rules(seed=seed, balance_max_run=balance_max_run)}
    return [pool[n] for n in names if n in pool]
```

- [ ] **Step 2: 更新配置**（`config/config.yaml` collaboration 域）

```yaml
collaboration:
  enabled: false
  rules_order: [mention, intent, continuation, relevance, balance, cooldown, random]
  balance:
    max_run: 2          # 防垄断：同角色连续发言超过该值且无更强信号时换人
  trigger_probability: 0.3
  trigger_global_cooldown: 20.0
  awareness:
    enabled: true
    max_partner_lines: 2
```

- [ ] **Step 3: 适配测试与装配**

`tests/test_collab_rules.py` 的 `test_make_rules_by_order_seed_and_unknown` 若断言默认顺序需同步；`arbitrator.py` 构造处的 `build_default_rules(seed=seed)` 签名兼容（`balance_max_run` 有默认值，不改调用）；检查 `test_collab_arbitrator.py` / `test_collab_coordinator.py` 是否有链顺序相关断言，按实际运行结果适配。

- [ ] **Step 4: 全量回归**

Run: `python -m pytest -q`
Expected: 全部通过（若有断言因链顺序变化失败，修正断言以匹配新结构语义）

- [ ] **Step 5: 提交**

```bash
git add src/orchestrators/collaboration/rules.py config/config.yaml tests/test_collab_rules.py tests/test_collab_arbitrator.py tests/test_collab_coordinator.py
git commit -m "feat(collab): 规则链加入结构规则并更新配置（continuation/balance）"
```

---

### Task 4: triggers 结构驱动接话增强

**Files:**
- Modify: `src/orchestrators/collaboration/triggers.py`
- Modify: `tests/test_collab_triggers.py`

- [ ] **Step 1: 写失败测试**

```python
def test_question_ending_triggers_direct_banter():
    tr = CollabTriggers(probability=0.0, global_cooldown=0.0,
                        present_roles={"yuki", "lilith"}, seed=42)
    props = tr.evaluate("yuki", "你们觉得这个故事怎么样？")
    assert props and props[0]["role"] == "lilith" and props[0]["kind"] == "banter"


def test_story_marker_boosts_probability():
    tr = CollabTriggers(probability=0.2, global_cooldown=0.0,
                        present_roles={"yuki", "lilith"}, seed=0)
    props = tr.evaluate("yuki", "那我给大家讲个故事吧")
    assert props  # 结构增强使 0.2 概率下必然触发


def test_plain_speech_keeps_probability():
    tr = CollabTriggers(probability=0.0, global_cooldown=0.0,
                        present_roles={"yuki", "lilith"}, seed=0)
    assert tr.evaluate("yuki", "今天天气不错") == []
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_collab_triggers.py::test_question_ending_triggers_direct_banter -v`
Expected: FAIL（probability=0 时仍产出 → 结构增强未实现）

- [ ] **Step 3: 实现**（`triggers.py`）

```python
_QUESTION_SUFFIXES = ("？", "?")
_STORY_MARKERS = ("故事", "笑话", "讲个", "哈哈", "好玩", "真的假的")


class CollabTriggers:
    # ... 既有 __init__ 不变 ...

    @staticmethod
    def _structural_effective_probability(probability: float, text: str) -> float:
        """结构驱动概率：提问必须被接（1.0）；故事/玩笑倾向被接（0.6）；否则用配置概率。"""
        t = (text or "").strip()
        if t.endswith(_QUESTION_SUFFIXES):
            return 1.0
        if any(m in t for m in _STORY_MARKERS):
            return max(probability, 0.6)
        return probability

    def evaluate(self, speaker, text):
        now = time.time()
        if now - self._last_trigger_at < self._cooldown:
            return []
        effective = self._structural_effective_probability(self._probability, text)
        if effective <= 0 or self._rng.random() > effective:
            return []
        others = [r for r in sorted(self._present) if r != speaker]
        if not others:
            return []
        self._last_trigger_at = now
        target = self._rng.choice(others)
        return [{"role": target, "kind": "banter", "reason": "speech-completed",
                 "ref_text": text}]
```

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_collab_triggers.py -v`
Expected: PASS（既有 9 项 + 新增 3 项；若 `test_probability_zero_disables` 用非结构文本则不受影响）

Run: `python -m pytest -q`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add src/orchestrators/collaboration/triggers.py tests/test_collab_triggers.py
git commit -m "feat(collab): 接话结构增强（提问必接/故事倾向接）"
```

---

### Task 5: 调参测量脚本更新 + 全量回归 + 收尾

**Files:**
- Modify: 临时目录 `tune_arbitration.py` / `eval_keywords.py`（不入库，仅测量）
- Run: 全量测试

- [ ] **Step 1: 更新测量脚本弹幕集**（追加结构信号弹幕）

在 `DANMAKU_SET` 组 4 后追加结构组：
```python
    # 组6 结构延续（模拟上轮 Yuki 刚讲完故事后的观众回应）
    ("讲个故事吧", 0.4),          # relevance:yuki
    ("这个故事真好听，再来一个", 0.4),  # continuation:yuki（依赖上轮 Yuki 发言在话轮历史中）
    ("然后呢然后呢", 0.4),         # continuation:yuki
```

- [ ] **Step 2: 跑基线对比**（V2 前后）

Run: `python tune_arbitration.py --probability 0.3 --cooldown 5.0`
Expected: `continuation:*` 规则出现在命中分布中，且同场景下 relevance/cooldown 命中占比下降

- [ ] **Step 3: 全量回归 + 冒烟**

Run: `python -m pytest -q`
Expected: 全部通过

- [ ] **Step 4: 提交**

```bash
git add -u  # 仅提交计划内文件（V2 收尾确认无额外改动）
git commit -m "test(collab): V2 结构规则层全量回归通过"   # 若无需提交则跳过，确认工作区仅含并行改动
```

---

# V3 · 紧迫度判断器（本期只出设计，不实现）

## 设计目标

把"规则链产生唯一胜者"升级为"**每个角色产出紧迫度，机制取最大**"——LLM 提议、机制裁决的单决策通道，护栏（互斥/冷却/预算/失败回退）全部保留。

## 模块设计（`src/orchestrators/collaboration/judge.py`，待实施）

```python
@dataclass
class UrgencyResult:
    urgencies: Dict[str, float]   # role -> 0..1
    silent: bool                  # 是否建议沉默（无角色回应）
    reason: str                   # 审计：judge 依据

class UrgencyJudge(Protocol):
    """紧迫度判断器协议：输入仲裁上下文，输出各角色紧迫度。"""
    def judge(self, ctx: ArbitrationContext) -> UrgencyResult: ...

class RulesJudge:
    """规则链映射为紧迫度：把现有规则链每个 verdict 的 confidence 作为该角色紧迫度；
    链尾无命中 → silent=True。零成本，是默认实现。"""

class LLMJudge:
    """轻量 LLM 判断：注入最近话轮 + 角色画像 + 弹幕 → 结构化输出
    {"yuki": 0.3, "lilith": 0.7, "silent": false}。
    预算控制：judge_budget_per_min 内调用；超预算/失败 → 回退 RulesJudge。"""
```

## 仲裁器改造点（`arbitrator.py`，待实施）

- 构造新增 `judge`（默认 `RulesJudge`）+ `judge_budget_per_min`。
- `arbitrate()` 改为：`result = judge.judge(ctx)` → `silent` 则返回 None；否则取 `urgencies` 最大者（平局回退 cooldown/random）→ `acquire`/入队/发布事件逻辑不变。
- 护栏不变：互斥、deferred 队列、priority 映射、speech:completed 释放排空。

## 配置（待实施）

```yaml
collaboration:
  judge: rules            # rules | llm（V3 切换点）
  llm_judge:
    budget_per_min: 4     # 每分钟最多 LLM 判断次数；超预算回退规则
    model: ""             # 缺省沿用 llm 主模型
```

## 验收标准（V3）

1. `judge: rules` 时行为与 V2 完全一致（回归测试）。
2. `judge: llm` 时紧迫度取最大者发言，silent 时不发言。
3. 预算耗尽/LLM 失败 → 自动回退 RulesJudge，链路不中断。
4. 互斥/冷却/排队护栏在两种 judge 下均生效（复用既有测试）。
5. 成本可观测：`collab:judge` 事件记录每次 judge 来源（rules/llm）与耗时。

## 实施顺序（V3 启动）

### Task 6: judge.py 协议 + RulesJudge（默认零行为变化）

**Files:**
- Create: `src/orchestrators/collaboration/judge.py`
- Create: `tests/test_collab_judge.py`

- [ ] **Step 1: 写失败测试**

```python
"""judge 单测（V3：紧迫度协议 + RulesJudge）。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.collaboration.judge import RulesJudge, UrgencyResult
from src.orchestrators.collaboration.rules import (
    ArbitrationContext, make_rules_by_order,
)


class FakeProfiles:
    def keywords_for(self, role):
        return {"yuki": {"topics": ["故事", "月亮"], "patterns": ["讲个故事"]},
                "lilith": {"topics": ["吐槽", "直播"], "patterns": []}}[role]


class FakeTT:
    def __init__(self, last=None):
        self.last = last or {"yuki": 100.0, "lilith": 50.0}

    def idle_seconds(self, role):
        return self.last.get(role, 0.0)

    def turn_history(self, limit=10):
        return []


def _ctx(text):
    return ArbitrationContext(text=text, user_name="观众", source="danmaku",
                              kind="danmaku", lead_role="yuki",
                              present_roles={"yuki", "lilith"},
                              profiles=FakeProfiles(), turn_tracker=FakeTT())


def test_rules_judge_returns_winner_urgency():
    rules = make_rules_by_order(["mention", "relevance", "random"], seed=1)
    r = RulesJudge(rules).judge(_ctx("@Lilith 你怎么看"))
    assert r.urgencies == {"lilith": 1.0}
    assert r.silent is False
    assert r.source == "rules"


def test_rules_judge_matches_chain_semantics():
    # 无 @、无关键词 → 落到链尾 random（与仲裁器既有行为一致，非 silent）
    rules = make_rules_by_order(["mention", "relevance", "random"], seed=1)
    r = RulesJudge(rules).judge(_ctx("随便聊聊"))
    assert r.silent is False
    assert len(r.urgencies) == 1                      # 仅胜者角色有紧迫度
    assert set(r.urgencies) <= {"yuki", "lilith"}
    assert list(r.urgencies.values())[0] == 0.5       # random 规则 confidence


def test_rules_judge_silent_on_empty_present():
    rules = make_rules_by_order(["mention", "random"], seed=1)
    ctx = _ctx("随便聊聊")
    ctx.present_roles = set()
    r = RulesJudge(rules).judge(ctx)
    assert r.silent is True and r.urgencies == {}
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_collab_judge.py -v`
Expected: FAIL（judge.py 不存在）

- [ ] **Step 3: 实现**（`src/orchestrators/collaboration/judge.py`）

```python
"""judge.py — 紧迫度判断器（V3：LLM 提议、机制裁决的单决策通道）。

judge 协议：输入仲裁上下文，输出各角色紧迫度 + 是否建议沉默 + 来源。
- RulesJudge：规则链映射（默认，零 LLM，行为与 V2 仲裁完全一致）。
- LLMJudge：轻量 LLM 判断（预算控制 + 失败回退 RulesJudge），Task 7 实现。

护栏不在此层：互斥/冷却/排队由 arbitrator 在 judge 之后统一执行。
"""
from dataclasses import dataclass
from typing import Dict, List, Protocol

from src.orchestrators.collaboration.rules import (
    ArbitrationContext, Rule, RuleVerdict,
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
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_collab_judge.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/orchestrators/collaboration/judge.py tests/test_collab_judge.py
git commit -m "feat(collab): 紧迫度协议 + RulesJudge（V3 判断器底座，零行为变化）"
```

---

### Task 7: LLMJudge + 预算/回退

**Files:**
- Modify: `src/orchestrators/collaboration/judge.py`（追加 LLMJudge）
- Modify: `tests/test_collab_judge.py`

- [ ] **Step 1: 写失败测试**

```python
class FakeLLM:
    """可控 LLM：按注入回复序列返回；可配置抛错。"""
    def __init__(self, replies=None, error=None):
        self._replies = list(replies or [])
        self._error = error
        self.calls = 0

    def _chat(self, payload):
        self.calls += 1
        if self._error:
            raise self._error
        if self._replies:
            reply = self._replies.pop(0)
        else:
            reply = '{"yuki": 0.2, "lilith": 0.8, "silent": false}'
        return {"ok": True, "data": {"reply": reply}}


def _judge_ctx(text="随便聊聊"):
    return _ctx(text)


def test_llm_judge_parses_urgencies():
    j = LLMJudge(FakeLLM(), FakeProfiles(), budget_per_min=10, rules_order=["random"], rng_seed=1)
    r = j.judge(_judge_ctx())
    assert r.urgencies["lilith"] == 0.8 and r.silent is False and r.source == "llm"


def test_llm_judge_silent_true():
    llm = FakeLLM(replies=['{"yuki": 0.1, "lilith": 0.1, "silent": true}'])
    j = LLMJudge(llm, FakeProfiles(), budget_per_min=10, rules_order=["random"], rng_seed=1)
    r = j.judge(_judge_ctx())
    assert r.silent is True


def test_llm_judge_fallback_on_error():
    j = LLMJudge(FakeLLM(error=RuntimeError("boom")), FakeProfiles(),
                 budget_per_min=10, rules_order=["random"], rng_seed=1)
    r = j.judge(_judge_ctx())
    assert r.source == "rules-fallback" and r.silent is False


def test_llm_judge_fallback_on_bad_json():
    j = LLMJudge(FakeLLM(replies=["这不是 JSON"]), FakeProfiles(),
                 budget_per_min=10, rules_order=["random"], rng_seed=1)
    r = j.judge(_judge_ctx())
    assert r.source == "rules-fallback"


def test_llm_judge_budget_exhausted_falls_back():
    llm = FakeLLM()
    j = LLMJudge(llm, FakeProfiles(), budget_per_min=2, rules_order=["random"], rng_seed=1)
    j.judge(_judge_ctx())
    j.judge(_judge_ctx())
    r = j.judge(_judge_ctx())   # 第 3 次：预算耗尽
    assert r.source == "rules-fallback"
    assert llm.calls == 2       # 未发起第 3 次 LLM 调用
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_collab_judge.py::test_llm_judge_parses_urgencies -v`
Expected: FAIL（LLMJudge 不存在）

- [ ] **Step 3: 实现**（`src/orchestrators/collaboration/judge.py` 追加）

```python
import json
import re
import threading
import time

from src.orchestrators.collaboration.rules import make_rules_by_order

DEFAULT_RULES_ORDER = ["mention", "intent", "continuation", "relevance",
                       "balance", "cooldown", "random"]
_JUDGE_SYSTEM = (
    "你是直播间双虚拟主播的发言权判断器。根据弹幕/情境判断 Yuki 与 Lilith "
    "谁更应当说话（或都不说话）。只输出 JSON，不要其它文字："
    '{"yuki": 0到1的紧迫度, "lilith": 0到1的紧迫度, "silent": true或false}'
)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class LLMJudge:
    """轻量 LLM 判断：注入弹幕 + 最近话轮 + 角色画像 → 结构化紧迫度。

    预算：budget_per_min 次/分钟（成功调用计数），超预算回退 RulesJudge。
    失败：LLM 异常 / JSON 解析失败 → 回退 RulesJudge（source=rules-fallback）。
    """

    def __init__(self, llm, profiles, budget_per_min: int = 4,
                 rules_order: Optional[List[str]] = None,
                 rng_seed: Optional[int] = None):
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
```

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_collab_judge.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/orchestrators/collaboration/judge.py tests/test_collab_judge.py
git commit -m "feat(collab): LLMJudge（紧迫度判断 + 预算控制 + 失败/超预算回退）"
```

---

### Task 8: arbitrator 接入 judge + 事件 + 配置 + 装配

**Files:**
- Modify: `src/orchestrators/collaboration/arbitrator.py`
- Modify: `src/orchestrators/collaboration/coordinator.py`（judge 透传）
- Modify: `src/shared/events.py`（`COLLAB_JUDGE` + ALL_EVENTS）
- Modify: `config/config.yaml`（`collaboration.judge` / `llm_judge`）
- Modify: `src/app.py`（装配 judge）
- Modify: `tests/test_collab_arbitrator.py` / `tests/test_collab_judge.py` / `tests/test_p2_app_boot.py`

- [ ] **Step 1: 写失败测试**（`tests/test_collab_arbitrator.py` 追加）

```python
def test_arbitrate_with_judge_llm_urgency():
    """LLM judge 模式：取紧迫度最大者。"""
    class FakeJudge:
        source = "test"
        def judge(self, ctx):
            return UrgencyResult({"yuki": 0.2, "lilith": 0.9}, False, "llm", "llm")
    arb = SpeakerArbitrator(profiles=FakeProfiles(), event_bus=EventBus(),
                            judge=FakeJudge())
    v = arb.arbitrate("danmaku", "随便聊聊", "观众", "danmaku")
    assert v.role == "lilith"


def test_arbitrate_judge_silent_no_speech():
    class SilentJudge:
        def judge(self, ctx):
            return UrgencyResult({}, True, "silent", "llm")
    arb = SpeakerArbitrator(profiles=FakeProfiles(), event_bus=EventBus(),
                            judge=SilentJudge())
    v = arb.arbitrate("danmaku", "随便聊聊", "观众", "danmaku")
    assert v.role is None
    assert arb._tt.current_speaker is None   # 未占用互斥


def test_arbitrate_publishes_judge_event():
    bus = EventBus()
    seen = []
    bus.subscribe(COLLAB_JUDGE, lambda event, **kw: seen.append(kw))
    arb = SpeakerArbitrator(profiles=FakeProfiles(), event_bus=bus)
    arb.arbitrate("danmaku", "@Lilith 你怎么看", "观众", "danmaku")
    assert seen and seen[0]["source"] == "rules"
    assert "latency" in seen[0]
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_collab_arbitrator.py::test_arbitrate_with_judge_llm_urgency -v`
Expected: FAIL（arbitrate 无 judge 参数）

- [ ] **Step 3: 实现**

3a) `src/shared/events.py`：新增 `COLLAB_JUDGE = "collab:judge"` 并加入 `ALL_EVENTS`（同步运行 `tests/test_events_schema.py` 确认 schema 校验通过）。

3b) `src/orchestrators/collaboration/arbitrator.py`：

```python
import random
import time

class SpeakerArbitrator:
    def __init__(self, profiles=None, event_bus=None, lead_role="yuki",
                 rules_order=None, seed=None, judge=None, turn_tracker=None):
        # ... 既有字段 ...
        self._rng = random.Random(seed)
        self._judge = judge or RulesJudge(self._rules)

    def set_judge(self, judge) -> None:
        """运行时替换判断器（app 装配 llm judge 用）。"""
        self._judge = judge

    def _select_winner(self, urgencies):
        top = max(urgencies.values())
        tied = [r for r, u in urgencies.items() if u == top]
        if len(tied) == 1:
            return tied[0]
        idle = {r: self._tt.idle_seconds(r) for r in tied}
        max_idle = max(idle.values())
        idle_winners = [r for r, v in idle.items() if v == max_idle]
        if len(idle_winners) == 1:
            return idle_winners[0]
        return self._rng.choice(sorted(idle_winners))

    def arbitrate(self, source, text, user_name, kind, requester_role="", ref_text=""):
        request_id = <沿用既有 arbitrate 内 request_id 生成逻辑，不变>
        ctx = ArbitrationContext(text=text, user_name=user_name, source=source,
                                 kind=kind, lead_role=self._lead_role,
                                 present_roles=self._present_roles(),
                                 profiles=self._profiles, turn_tracker=self._tt)
        start = time.perf_counter()
        result = self._judge.judge(ctx)
        latency = time.perf_counter() - start
        self._publish_judge(result, latency)
        hit = result.reason
        if result.silent or not result.urgencies:
            return ArbitrationVerdict(None, hit, request_id)
        winner = self._select_winner(result.urgencies)
        # 互斥/入队/优先级/发布事件：与既有逻辑完全一致（acquire 失败 → enqueue + deferred）
        ...

    def _publish_judge(self, result: UrgencyResult, latency: float) -> None:
        """发布 collab:judge 事件（成本可观测：来源 + 耗时 + 紧迫度）。"""
        self._event_bus.publish(
            COLLAB_JUDGE,
            urgencies=result.urgencies, silent=result.silent,
            reason=result.reason, source=result.source, latency=round(latency, 4))
```

3c) `coordinator.py`：构造新增 `judge=None` 参数并透传 `SpeakerArbitrator(..., judge=judge)`。

3d) `config/config.yaml`：

```yaml
collaboration:
  judge: rules            # rules | llm（V3 切换点）
  llm_judge:
    budget_per_min: 4     # 每分钟最多 LLM 判断次数；超预算回退规则
```

3e) `src/app.py`（collaboration 装配块内，`collaboration.start()` 之前）：

```python
judge_mode = str(collab_cfg.get("judge", "rules"))
judge_obj = None
if judge_mode == "llm":
    from src.orchestrators.collaboration.judge import LLMJudge
    judge_cfg = collab_cfg.get("llm_judge") or {}
    judge_obj = LLMJudge(
        llm_orch, collab_profiles,
        budget_per_min=int(judge_cfg.get("budget_per_min", 4)),
        rules_order=collab_cfg.get("rules_order"))
```
并将 `judge=judge_obj` 传入 `CollaborationCoordinator`。

- [ ] **Step 4: 回归 + 新测试**

Run: `python -m pytest tests/test_collab_arbitrator.py tests/test_collab_judge.py tests/test_collab_coordinator.py tests/test_events_schema.py tests/test_p2_app_boot.py -q`
Expected: PASS（默认 judge=RulesJudge 时既有行为零变化；events schema 校验含新事件）

Run: `python -m pytest -q`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add src/orchestrators/collaboration/arbitrator.py src/orchestrators/collaboration/coordinator.py src/shared/events.py config/config.yaml src/app.py tests/test_collab_arbitrator.py tests/test_collab_judge.py
git commit -m "feat(collab): arbitrator 接入紧迫度判断器（judge 可插拔 + 事件 + 配置）"
```

> 注意：`arbitrator.py` / `coordinator.py` / `events.py` / `app.py` 可能含并行工作改动，提交前用"备份→摘除→提交→恢复"模式确保零污染（参照 T16 做法）；`events.py` 若含并行未提交修改，先确认其归属。

---

## V3 验收标准（复述）

1. `judge: rules` 时行为与 V2 完全一致（默认 RulesJudge，全量回归）。
2. `judge: llm` 时紧迫度取最大者发言，silent 时不发言（mock LLM 测试）。
3. 预算耗尽/LLM 失败 → 自动回退 RulesJudge，链路不中断（`rules-fallback` 事件可见）。
4. 互斥/冷却/排队护栏在两种 judge 下均生效（复用既有测试）。
5. 成本可观测：`collab:judge` 事件记录 source（rules/llm）与耗时 latency。

---

## 自审记录

- **规格覆盖**：V2 覆盖"随机性治本"（结构规则 + 接话增强）；V3 覆盖"灵活性上限"（LLM 提议-机制裁决）；护栏/互斥/泛用性保持既有架构不动。
- **占位符**：V3 为设计章节（用户明确"先做 V2"），无 TBD；V2 各任务含完整代码。
- **类型一致**：`ContinuationRule/BalanceRule.evaluate(ctx) -> RuleVerdict` 与既有 Rule 基类一致；`turn_tracker.turn_history(limit)` 与既有实现一致；`CollabTriggers.evaluate(speaker, text)` 签名不变。
- **风险**：链顺序变化可能影响既有仲裁测试断言，Task 3 已注明按实际结果适配；`FakeTT` 补 `turn_history` 保持测试桩与真实接口一致。
