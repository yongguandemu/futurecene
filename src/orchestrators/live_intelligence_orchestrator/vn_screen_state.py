"""模块内容清单 — vn_screen_state

## 1. 模块身份标识
- 所属调度官：live_intelligence_orchestrator
- 能力名：（纯数据结构，供 commentary_policy / pace 使用）
- 引擎名：（单实现）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| state | 否 | "unknown" | str ∈ {dialogue,choice,menu,puzzle,transition,cg,unknown} | 画面状态 |
| text | 否 | "" | str | 当前画面识别文本 |

## 3. 输入契约
- 由 commentary_policy / pace 消费，无需外部输入。

## 4. 输出契约
- 无独立输出，作为数据载体现身。

## 5. 依赖声明
- 外部服务：无
- 内部模块：无
- 预先配置：无

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 无 | 纯数据类 | - |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | - | 纯数据类，无生命周期 |

## 8. 领域状态说明
- 状态项：state / text
- 持久化：无
- 恢复：无
"""
from dataclasses import dataclass, field


@dataclass
class VNScreenState:
    """VN 画面状态（供解说策略与节奏控制器共享）。"""
    state: str = "unknown"  # dialogue/choice/menu/puzzle/transition/cg/unknown
    text: str = ""          # 当前画面识别文本