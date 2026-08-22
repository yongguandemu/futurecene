"""decision_policy.py — 决策分级与上抛策略（共享层）

把「什么决策留在哪一层、什么必须上抛到总脑」从隐含行为变成显式可测的规则。

四级决策模型（规格书 5.6）：
- L0 反射：硬规则直接拦截/放行（脏话过滤、@点名），零 LLM、零咨询
- L1 域内自治：只影响本域、可逆、低成本，调度官自己拍板（插火把、切歌、截图）
- L2 仲裁上抛：跨域冲突或资源互斥，仲裁器确定性规则链裁决（谁发言）
- L3 总脑编排：需要全局上下文或高风险不可逆，必须到指挥官（回复弹幕、开播/下播、切换角色）

classify() 实现三问框架，顺序判定：
1. 有硬规则命中？→ L0（拦截/放行）
2. 决策归属矩阵有显式声明？→ 按矩阵层
3. 只影响本域且可逆且低成本？→ L1 域内自决
4. 需要全局上下文 / 高风险 / 不可逆？→ L3 总脑
5. 跨域冲突？→ L2 仲裁
6. 兜底 → L1

新增能力必须先在 DECISION_MATRIX 登记归属层（支持 * 通配前缀），
未登记的能力走三问推断，保证每个调度官开发时都知道自己的决策边界。

# 模块内容清单 — decision_policy

## 1. 模块身份标识
- 所属调度官：shared（指挥官与全部调度官共用）
- 能力名：decision:classify（共享策略）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| DECISION_MATRIX | 是 | 见下 | dict | 决策归属矩阵（capability → 层/处置/依据），支持 * 通配前缀 |
| 无实例配置 | - | - | - | 纯函数 + 常量模块 |

## 3. 输入契约
- 输入格式：`classify(request: DecisionRequest) -> DecisionVerdict`
- request：DecisionRequest（domain/capability/affected_domains/reversible/risk/needs_global_context/has_hard_rule/rule_hit）

## 4. 输出契约
- 成功：返回 DecisionVerdict（layer/outcome/reason/matched_rule/source）
- 失败：无异常路径（未登记能力走推断，兜底 L1）
- 事件：无（决策日志由 decision_log 负责）

## 5. 依赖声明
- 外部服务：无
- 内部模块：dataclasses、enum（纯标准库）
- 预先配置：矩阵随模块加载生效

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 无（纯函数） | - | 未登记能力按三问推断，不抛异常 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start/stop | 否 | 无（纯函数 + 常量矩阵） |

## 8. 领域状态说明
- 状态项：DECISION_MATRIX（只读常量矩阵）
- 持久化：无
- 恢复：无状态，随调随用
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple


class DecisionLayer(str, Enum):
    """四级决策模型。"""

    L0_REFLECT = "L0"      # 反射：硬规则拦截/放行，零咨询
    L1_AUTONOMY = "L1"     # 域内自治：调度官自己拍板
    L2_ARBITRATE = "L2"    # 仲裁上抛：跨域冲突，确定性规则链
    L3_BRAIN = "L3"        # 总脑编排：全局上下文 / 高风险不可逆


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class DecisionRequest:
    """一次待分类的决策请求。"""

    domain: str                 # 发起决策的调度官，如 "experience"
    capability: str             # 能力名，如 "music:next"
    affected_domains: Tuple[str, ...] = ()   # 影响的其它域（空 = 只影响本域）
    reversible: bool = True     # 是否可逆（插火把可逆，下播不可逆）
    risk: str = RiskLevel.LOW   # 风险等级 low/medium/high
    needs_global_context: bool = False  # 是否需要角色/会话/系统能力等全局上下文
    has_hard_rule: bool = False # 是否有硬规则可判
    rule_hit: str = ""          # 命中的硬规则名（has_hard_rule=True 时）

    @classmethod
    def of(cls, capability: str, domain: str = "",
           affected_domains: Tuple[str, ...] = (), reversible: bool = True,
           risk: str = RiskLevel.LOW, needs_global_context: bool = False,
           has_hard_rule: bool = False, rule_hit: str = "") -> "DecisionRequest":
        """便捷构造：capability 的 `:` 前缀自动作为 domain。"""
        return cls(domain=domain or capability.split(":", 1)[0],
                   capability=capability,
                   affected_domains=affected_domains, reversible=reversible,
                   risk=risk, needs_global_context=needs_global_context,
                   has_hard_rule=has_hard_rule, rule_hit=rule_hit)


@dataclass
class DecisionVerdict:
    """分类结果：决策归属层 + 处置方式 + 依据。"""

    layer: str                  # L0 / L1 / L2 / L3
    outcome: str                # execute / block / escalate / silent
    reason: str                 # 人类可读依据
    matched_rule: str = ""      # 命中的硬规则（L0 时）
    source: str = ""            # matrix（矩阵显式）/ hard_rule（硬规则）/ infer（三问推断）


def _entry(layer: str, outcome: str, reason: str) -> Dict[str, str]:
    return {"layer": layer, "outcome": outcome, "reason": reason}


# ========== 决策归属矩阵（规格书 5.6.3，代码唯一显式来源） ==========
# 键支持 * 通配前缀（如 "music:*"），匹配时精确键优先、通配按最长前缀。
# 依据真实能力名（各调度官 registry.py / 模块内容清单），新增能力必须在此登记。
DECISION_MATRIX: Dict[str, Dict[str, str]] = {
    # ---------- L0 反射：硬规则拦截 ----------
    # 注：safety:check_input / safety:check_output 已于 ADR-007 退役（信任厂商
    # 安全系统，内容安全不再做本地硬规则过滤），仅保留规则热加载能力。
    "safety:reload_rules": _entry("L0", "execute", "规则热加载属安全域内部"),

    # ---------- L1 域内自治 ----------
    "screen:capture": _entry("L1", "execute", "截图属屏幕控制域内操作"),
    "screen:click": _entry("L1", "execute", "点击属屏幕控制域内操作"),
    "screen:keypress": _entry("L1", "execute", "按键属屏幕控制域内操作"),
    "screen:execute_plan": _entry("L1", "execute", "屏幕操作计划域内执行"),
    "screen:move": _entry("L1", "execute", "鼠标移动属屏幕控制域内操作"),
    "screen:scroll": _entry("L1", "execute", "滚轮滚动属屏幕控制域内操作"),
    "screen:drag": _entry("L1", "execute", "拖拽属屏幕控制域内操作"),
    "screen:template_match": _entry("L1", "execute", "模板匹配识别属屏幕控制域内操作"),
    "screen:cursor": _entry("L1", "execute", "虚拟光标控制属屏幕控制域内操作"),
    "screen:cursor_state": _entry("L1", "execute", "虚拟光标状态查询域内操作"),
    "screen:*": _entry("L1", "execute", "屏幕控制域内操作"),
    "music:*": _entry("L1", "execute", "播放控制/点歌域内自治（切歌等）"),
    "experience:*": _entry("L1", "execute", "经验决策域内闭环（插火把等）"),
    "game:vn_state": _entry("L1", "execute", "VN 画面轮询属域内行为"),
    "game:op_state": _entry("L1", "execute", "游戏操作循环状态查询域内行为"),
    "game:op_plan": _entry("L1", "execute", "游戏操作指令规划域内行为"),
    "game:op_command": _entry("L1", "execute", "游戏单条指令执行域内可逆操作"),
    "bilibili:connect": _entry("L1", "execute", "平台连接域内管理"),
    "bilibili:disconnect": _entry("L1", "execute", "平台断开域内管理"),
    "bilibili:send_message": _entry("L1", "execute", "消息发送是已决策的执行通道"),
    "bilibili:get_stream_code": _entry("L1", "execute", "取推流码域内操作"),
    "adapter:obs_connect": _entry("L1", "execute", "OBS 连接域内管理"),
    "adapter:obs_scene": _entry("L1", "execute", "OBS 场景切换域内可逆操作"),
    "adapter:obs_screenshot": _entry("L1", "execute", "OBS 截图域内操作"),
    "adapter:obs_source": _entry("L1", "execute", "OBS 源管理域内操作"),
    "adapter:qq_connect": _entry("L1", "execute", "QQ 连接域内管理"),
    "adapter:qq_send_*": _entry("L1", "execute", "QQ 消息发送是执行通道"),
    "adapter:vts_*": _entry("L1", "execute", "VTS 参数/表情域内操作"),
    "stream:alive": _entry("L1", "execute", "推流状态查询"),
    "stream:fetch_code": _entry("L1", "execute", "取推流码域内操作"),
    "stream:launch_app": _entry("L1", "execute", "应用进程管理域内操作"),
    "stream:app_*": _entry("L1", "execute", "应用进程管理域内操作"),
    "obs:sources": _entry("L1", "execute", "OBS 源清单查询域内操作"),
    "obs:open": _entry("L1", "execute", "打开浏览器源域内可逆操作"),

    # ---------- L2 仲裁上抛：跨域冲突 ----------
    "collab:*": _entry("L2", "escalate", "发言权/协作仲裁（确定性规则链）"),
    "game:vn_start": _entry("L2", "escalate", "启动 VN 陪看涉屏幕/解说多域"),
    "game:vn_stop": _entry("L2", "escalate", "停止 VN 陪看涉屏幕/解说多域"),
    "game:mc_start": _entry("L2", "escalate", "启动 MC 实况涉屏幕/推流/解说多域"),
    "game:mc_stop": _entry("L2", "escalate", "停止 MC 实况涉屏幕/推流/解说多域"),
    "game:op_start": _entry("L2", "escalate", "开启 AI 自动操作涉屏幕/解说多域"),
    "game:op_stop": _entry("L2", "escalate", "关闭 AI 自动操作涉屏幕/解说多域"),

    # ---------- L3 总脑编排：全局上下文 / 高风险不可逆 ----------
    "llm:chat": _entry("L3", "escalate", "对话生成需全局上下文（回复弹幕）"),
    "llm:*": _entry("L3", "escalate", "LLM 生成需全局上下文"),
    "game:commentary": _entry("L3", "escalate", "解说生成需全局情境上下文"),
    "adapter:obs_stream": _entry("L3", "escalate", "OBS 推流启停即开播/下播，高风险不可逆"),
    "stream:start": _entry("L3", "escalate", "开播高风险不可逆"),
    "stream:stop": _entry("L3", "escalate", "下播高风险不可逆"),
    "session:switch": _entry("L3", "escalate", "切换角色/会话需全局上下文"),
}


def matrix_lookup(capability: str) -> Optional[Dict[str, str]]:
    """按能力名查矩阵：精确键优先，通配按最长前缀。未命中返回 None。"""
    exact = DECISION_MATRIX.get(capability)
    if exact is not None:
        return exact
    best, best_len = None, -1
    for key, val in DECISION_MATRIX.items():
        if key.endswith("*") and capability.startswith(key[:-1]):
            if len(key) > best_len:
                best, best_len = val, len(key)
    return best


def classify(request: DecisionRequest) -> DecisionVerdict:
    """三问框架显式实现：硬规则 → 矩阵 → 域内推断 → 总脑/仲裁。"""
    # 1. 硬规则命中 → L0（反射拦截/放行，零咨询）
    if request.has_hard_rule and request.rule_hit:
        return DecisionVerdict(layer=DecisionLayer.L0_REFLECT, outcome="block",
                               reason="硬规则命中: {}".format(request.rule_hit),
                               matched_rule=request.rule_hit, source="hard_rule")
    # 2. 决策归属矩阵显式声明 → 按矩阵
    entry = matrix_lookup(request.capability)
    if entry is not None:
        return DecisionVerdict(layer=entry["layer"], outcome=entry["outcome"],
                               reason=entry["reason"], source="matrix")
    # 3. 三问推断
    local_and_safe = (not request.affected_domains and request.reversible
                      and request.risk == RiskLevel.LOW
                      and not request.needs_global_context)
    if local_and_safe:
        return DecisionVerdict(layer=DecisionLayer.L1_AUTONOMY, outcome="execute",
                               reason="仅影响本域且可逆低成本，域内自决",
                               source="infer")
    if (request.needs_global_context or request.risk == RiskLevel.HIGH
            or not request.reversible):
        return DecisionVerdict(layer=DecisionLayer.L3_BRAIN, outcome="escalate",
                               reason="需全局上下文或高风险不可逆，上抛总脑",
                               source="infer")
    if request.affected_domains:
        return DecisionVerdict(layer=DecisionLayer.L2_ARBITRATE, outcome="escalate",
                               reason="跨域冲突需仲裁",
                               source="infer")
    return DecisionVerdict(layer=DecisionLayer.L1_AUTONOMY, outcome="execute",
                           reason="兜底：域内自决", source="infer")


def classify_capability(capability: str, domain: str = "",
                        affected_domains: Tuple[str, ...] = (),
                        reversible: bool = True, risk: str = RiskLevel.LOW,
                        needs_global_context: bool = False,
                        has_hard_rule: bool = False,
                        rule_hit: str = "") -> DecisionVerdict:
    """便捷入口：直接按能力名分类（内部构造 DecisionRequest）。"""
    return classify(DecisionRequest.of(
        capability, domain, affected_domains, reversible, risk,
        needs_global_context, has_hard_rule, rule_hit))


def matrix_rows() -> list:
    """矩阵行列表（规格书 5.6.3 表格的代码镜像，供测试与文档一致性校验）。"""
    return [{"capability": k, **v} for k, v in sorted(DECISION_MATRIX.items())]
