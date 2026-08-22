"""commander/input — 输入分层分发域（总控调度化）

指挥官内部服务：InputClassifier / PriorityQueue / DistributionRouter / ContextAggregator。
不注册调度官能力；被 command 入口与 danmaku 管线消费。

# 模块内容清单 — input 域 __init__

## 1. 模块身份标识
- 所属调度官：commander（input 域）
- 能力名：无

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| input.dispatch_mode | 否 | direct | direct/priority/adaptive | 总控分发模式（config.yaml，P1 实现 direct+priority） |

## 3. 输入契约
- InputClassifier.classify(...)；PriorityQueue.push/pop；DistributionRouter.route(...)；ContextAggregator.build(...)

## 4. 输出契约
- 见各子模块契约

## 5. 依赖声明
- 内部模块：input_classifier / priority_queue / distribution_router / context_aggregator

## 6. 错误定义
- 见各子模块

## 7. 生命周期方法
- 无（被动服务）

## 8. 领域状态说明
- 状态项：无（队列状态在 PriorityQueue 实例）
- 持久化：无
"""
from src.commander.input.input_classifier import (
    InputClassifier, InputEnvelope, InputType, PRIORITY,
)
from src.commander.input.priority_queue import PriorityQueue
from src.commander.input.distribution_router import DistributionRouter
from src.commander.input.context_aggregator import ContextAggregator

__all__ = ["InputClassifier", "InputEnvelope", "InputType", "PRIORITY",
           "PriorityQueue", "DistributionRouter", "ContextAggregator"]
