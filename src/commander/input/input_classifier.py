"""input_classifier.py — 输入分类（总控调度化，规格 2026-08-22 任务一）

五类输入统一分类与优先级标签：
- operator（P0，操作者，可插队/带身份标记）
- audience（P1，观众：弹幕/礼物/小游戏）
- external_app（P2，外部应用：屏幕控制/实况状态）
- system_loop（P3，系统自循环，带循环深度标记）
- reference（不排队，作上下文参考）

# 模块内容清单 — input_classifier

## 1. 模块身份标识
- 所属调度官：commander（input 域）
- 能力名：无（指挥官内部服务，被 command/danmaku 入口消费）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| 无 | - | - | - | 纯函数式分类（无实例配置） |

## 3. 输入契约
- classify(text="", source="", event="", kind="", loop_depth=0, operator_id="") -> InputEnvelope

## 4. 输出契约
- 成功：InputEnvelope（input_type/priority/source/payload/operator_id/loop_depth/meta）
- 失败：无异常路径（未知来源回退 audience）

## 5. 依赖声明
- 外部服务：无
- 内部模块：dataclasses、enum、typing

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 无 | - | 未知来源回退 audience |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | - | 无状态，随调随用 |

## 8. 领域状态说明
- 状态项：无
- 持久化：无
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict


class InputType(str, Enum):
    OPERATOR = "operator"          # P0 操作者
    AUDIENCE = "audience"          # P1 观众
    EXTERNAL_APP = "external_app"  # P2 外部应用
    SYSTEM_LOOP = "system_loop"    # P3 系统自循环
    REFERENCE = "reference"        # 不排队，上下文参考


PRIORITY = {
    InputType.OPERATOR: 0,
    InputType.AUDIENCE: 1,
    InputType.EXTERNAL_APP: 2,
    InputType.SYSTEM_LOOP: 3,
    InputType.REFERENCE: -1,       # -1 表示不排队
}

# 外部应用事件前缀 → external_app
_EXTERNAL_EVENTS = ("screen", "game", "obs", "stream", "live2d", "music")


@dataclass
class InputEnvelope:
    """一次已分类的输入。"""
    input_type: str
    priority: int
    source: str
    payload: Dict[str, Any] = field(default_factory=dict)
    operator_id: str = ""       # operator 身份标记
    loop_depth: int = 0         # 系统循环深度
    meta: Dict[str, Any] = field(default_factory=dict)


class InputClassifier:
    """五类输入识别 + 优先级标签。"""

    @staticmethod
    def classify(text: str = "", source: str = "", event: str = "",
                  kind: str = "", loop_depth: int = 0,
                  operator_id: str = "", **kw) -> InputEnvelope:
        text = (text or "").strip()
        event = event or ""
        source = (source or "").strip()

        # reference：显式 kind 标记（世界书/记忆/脚本查询类）
        if kind == "reference":
            return InputEnvelope(InputType.REFERENCE, PRIORITY[InputType.REFERENCE],
                                 source, {"text": text, **kw}, loop_depth=loop_depth)

        # operator：命令入口 source=command；或 ! 前缀指令
        if source == "command" or text.startswith("!"):
            return InputEnvelope(InputType.OPERATOR, PRIORITY[InputType.OPERATOR],
                                 source, {"text": text, **kw},
                                 operator_id=operator_id or "user", loop_depth=loop_depth)

        # system_loop：显式 source 或携带循环深度
        if source == "system_loop" or loop_depth > 0:
            return InputEnvelope(InputType.SYSTEM_LOOP, PRIORITY[InputType.SYSTEM_LOOP],
                                 source, {"text": text, **kw}, loop_depth=loop_depth)

        # external_app：事件前缀匹配
        if event.startswith(_EXTERNAL_EVENTS):
            return InputEnvelope(InputType.EXTERNAL_APP, PRIORITY[InputType.EXTERNAL_APP],
                                 source, {"text": text, "event": event, **kw})

        # audience：观众事件或其余来源
        return InputEnvelope(InputType.AUDIENCE, PRIORITY[InputType.AUDIENCE],
                             source, {"text": text, "event": event, **kw})
