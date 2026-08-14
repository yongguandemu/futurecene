"""arbitrator.py — 发言权仲裁器（核心）。

arbitrate(source, text, ...) → 依次应用规则链；当前有人发言时请求入待发队列，
verdict.deferred=True 表示"排队待发"，False 表示放行或无人回应。
发布 speech:arbitrated（role/rule_hit/request_id/deferred）供前端展示与调试。
零 LLM：规则全部为确定性文本规则（rules.py）。

# 模块内容清单 — arbitrator

## 1. 模块身份标识
- 所属调度官：collaboration（多角色协作域）
- 能力名：collab:arbitrate（发言权仲裁，间接经 coordinator 调用）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| rules_order | 否 | 无 | list[str] | 规则链顺序（缺省 build_default_rules：mention>intent>relevance>cooldown>random） |
| lead_role | 否 | "yuki" | str | 主控角色（intent 规则命中时放行） |
| seed | 否 | None | int | 随机种子（random 规则可复现） |
| present_roles | 否 | None | set[str] | 在场角色（ADR-001 单一来源，None 回退 profiles/默认双人组） |
| profiles | 否 | None | CharacterProfileLoader 兼容 | 角色关键词（relevance 规则用） |

## 3. 输入契约
- 输入格式：`arbitrate(source, text, user_name, kind, requester_role, ref_text)` -> ArbitrationVerdict
- source：str，消息来源（danmaku/collab/active）
- text：str，发言文本；user_name：str，观众名
- kind：str ∈ {danmaku, collab, active}；ref_text：str，引用文本

## 4. 输出契约
- 成功：返回 ArbitrationVerdict(role, rule_hit, request_id, deferred)；放行时 role 非 None
- 失败：规则链未命中返回 role=None（静默，合法决策）；互斥占用返回 role=None 且 deferred=True（入队待发）
- 事件：发布 `speech:arbitrated`（role/rule_hit/request_id/deferred）；决策日志见 decision_log

## 5. 依赖声明
- 外部服务：无
- 内部模块：`rules`（规则链）、`turn_tracker`（互斥/队列）、`shared.events.SPEECH_ARBITRATED`、`shared.decision_log`（可选）
- 预先配置：无（rules_order 缺省时自动构建默认规则链）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 规则异常 | 单条规则 evaluate 抛异常 | 由规则实现方捕获，仲裁器不中断 |
| 事件发布失败 | event_bus 异常 | 记录警告，不阻断仲裁 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start/stop | 否 | 无（由 coordinator 持有并调用） |
| set_profiles / set_lead_role / set_present_roles | 是 | 运行时更新角色配置 |

## 8. 领域状态说明
- 状态项：`_rules`（规则链）、`_tt`（话轮追踪引用）、`_lead_role`、`_profiles`、`_present`（在场角色）
- 持久化：无
- 恢复：无状态持久化；互斥与队列状态由 turn_tracker 持有
"""
import logging
import random
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional

from src.orchestrators.collaboration.judge import RulesJudge, UrgencyResult
from src.orchestrators.collaboration.rules import (
    ArbitrationContext, Rule, build_default_rules, make_rules_by_order,
)
from src.orchestrators.collaboration.turn_tracker import TurnTracker
from src.shared.decision_log import (
    OUTCOME_DEFERRED, OUTCOME_EXECUTED, OUTCOME_NO_ACTION, record_decision,
)
from src.shared.events import COLLAB_JUDGE, SPEECH_ARBITRATED

logger = logging.getLogger(__name__)


@dataclass
class ArbitrationVerdict:
    role: Optional[str]
    rule_hit: str = ""
    request_id: str = ""
    deferred: bool = False          # True=排队待发（互斥忙）；False=放行或无人回应


class SpeakerArbitrator:
    def __init__(self, event_bus, turn_tracker: Optional[TurnTracker] = None,
                 profiles=None, lead_role: str = "yuki",
                 rules_order: Optional[List[str]] = None,
                 present_roles: Optional[set] = None,
                 seed: Optional[int] = None,
                 judge=None):
        self._event_bus = event_bus
        self._tt = turn_tracker or TurnTracker()
        self._profiles = profiles
        self._lead_role = lead_role
        # 在场模型单一来源（ADR-001，会话状态归指挥官）：注入 session.present_roles
        # 副本；None 表示未注入，_present_roles() 回退 profiles.all_roles()/默认双人组。
        self._present = (set(present_roles) if present_roles is not None else None)
        self._rules: List[Rule] = (make_rules_by_order(rules_order, seed=seed)
                                   if rules_order else build_default_rules(seed=seed))
        self._rng = random.Random(seed)
        # V3 判断器：默认 RulesJudge（规则链映射，行为与 V2 完全一致）；
        # judge=LLMJudge 时由 LLM 产出紧迫度（提议），本类负责裁决（机制）。
        self._judge = judge or RulesJudge(self._rules)

    def set_judge(self, judge) -> None:
        """运行时替换判断器（app 装配 llm judge 用）。"""
        self._judge = judge

    def set_profiles(self, profiles) -> None:
        self._profiles = profiles

    def set_present_roles(self, present_roles) -> None:
        """注入在场角色集合（session 单源同步；coordinator 订阅 presence_changed 时调用）。

        None 表示恢复未注入状态（回退 profiles.all_roles()）；空集即空集（保留）。
        """
        self._present = (set(present_roles) if present_roles is not None else None)

    def set_lead_role(self, role: str) -> None:
        self._lead_role = role

    def arbitrate(self, source: str, text: str, user_name: str = "",
                  kind: str = "danmaku", requester_role: str = "",
                  ref_text: str = "") -> ArbitrationVerdict:
        request_id = uuid.uuid4().hex[:8]
        present = self._present_roles()
        ctx = ArbitrationContext(text=text, user_name=user_name, source=source,
                                 kind=kind, lead_role=self._lead_role,
                                 present_roles=present, profiles=self._profiles,
                                 turn_tracker=self._tt)
        # V3：判断器产出紧迫度（LLM 提议 / 规则链映射），本类负责裁决（机制）。
        start = time.perf_counter()
        result: UrgencyResult = self._judge.judge(ctx)
        latency = time.perf_counter() - start
        self._publish_judge(result, latency)
        hit = result.reason
        winner = None
        if not result.silent and result.urgencies:
            winner = self._select_winner(result.urgencies)
        if winner is None:
            # 决策日志：判断器建议沉默 / 全链未命中 = 显式的「决定不回应」（区别于没收到）
            record_decision(source="arbitrator", outcome=OUTCOME_NO_ACTION,
                            reason_code="arbitrate_no_winner",
                            layer="L2", capability="collab:arbitrate",
                            detail="判断器未产出发言者，静默不回应: hit={}, silent={}".format(
                                hit, result.silent),
                            decision_id=request_id)
            return ArbitrationVerdict(None, hit, request_id, deferred=False)

        request = {"role": winner, "priority": self._priority(kind, hit),
                   "request_id": request_id, "text": text, "ref_text": ref_text}
        if not self._tt.acquire(winner):
            self._tt.enqueue(request)     # 有人正发言：入队等待
            self._publish(winner, hit, request_id, deferred=True)
            record_decision(source="arbitrator", outcome=OUTCOME_DEFERRED,
                            reason_code="speech_queue_deferred",
                            layer="L2", capability="collab:arbitrate",
                            detail="{} 获发言权但互斥中，入队待发: {}".format(winner, hit),
                            decision_id=request_id)
            return ArbitrationVerdict(None, hit, request_id, deferred=True)
        self._publish(winner, hit, request_id, deferred=False)
        record_decision(source="arbitrator", outcome=OUTCOME_EXECUTED,
                        reason_code="arbitrated",
                        layer="L2", capability="collab:arbitrate",
                        detail="{} 获发言权: {}".format(winner, hit),
                        decision_id=request_id)
        return ArbitrationVerdict(winner, hit, request_id, deferred=False)

    def _select_winner(self, urgencies: dict) -> Optional[str]:
        """机制裁决：紧迫度取最大者；平局 → 闲置最久者优先；仍平局 → seed 随机。"""
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

    def _publish_judge(self, result: UrgencyResult, latency: float) -> None:
        """发布 collab:judge 事件（成本可观测：来源 + 耗时 + 紧迫度）。"""
        try:
            self._event_bus.publish(
                COLLAB_JUDGE,
                urgencies=result.urgencies, silent=result.silent,
                reason=result.reason, source=result.source,
                latency=round(latency, 4))
        except Exception as e:
            logger.warning("[Arbitrator] 发布判断事件失败: %s", e)

    @staticmethod
    def _priority(kind: str, hit: str) -> int:
        if hit.startswith("mention"):
            return 0
        if hit.startswith("intent") or hit.startswith("command"):
            return 1
        if hit.startswith("relevance"):
            return 2
        if kind == "collab":
            return 3
        if kind == "active":
            return 4
        return 5

    def _present_roles(self) -> set:
        # 注入值优先（session 单源）；未注入时回退 profiles.all_roles()，再回退默认双人组
        if self._present is not None:
            return set(self._present)
        if self._profiles is not None and hasattr(self._profiles, "all_roles"):
            return set(self._profiles.all_roles())
        return {"yuki", "lilith"}

    def _publish(self, role: str, rule_hit: str, request_id: str,
                 deferred: bool) -> None:
        try:
            self._event_bus.publish(SPEECH_ARBITRATED, role=role,
                                    rule_hit=rule_hit, request_id=request_id,
                                    deferred=deferred)
        except Exception as e:
            logger.warning("[Arbitrator] 发布仲裁事件失败: %s", e)
