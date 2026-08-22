# 任务一：信息输入分层分发（input 域）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在指挥官层新增 input 域：五类输入统一分类/排队/分发，总控调度化基础（operator 直通、audience 可排队、自循环带深度标记）。

**Architecture:** 新增 `src/commander/input/`（InputClassifier / PriorityQueue / DistributionRouter / ContextAggregator）。direct 模式（默认，开关关）保持现有链路行为不变；priority 模式（开关开）时弹幕入口经队列消费。operator 命令入口始终直通并带身份标记。事件注册 `input:classified/queued/routed`，上下文复用 `CONTEXT_SNAPSHOT_READY`。

**Tech Stack:** Python 3.10 / asyncio / threading / EventBus（同步发布-订阅）/ pytest

**参考规格：** `docs/superpowers/specs/2026-08-22-five-phase-upgrade-design.md` 第 1 章

---

### Task 1: 事件注册（events.py）

**Files:**
- Modify: `src/shared/events.py`（input 域事件常量 + ALL_EVENTS）

- [ ] **Step 1: 在 events.py 增加 input 域事件常量（放在「多角色协作域」之后）**

```python
# ========== 输入分发域（总控调度化） ==========
INPUT_CLASSIFIED = "input:classified"                 # 输入已分类（type/priority/operator_id/loop_depth）
INPUT_QUEUED = "input:queued"                         # 输入已入队（priority 模式）
INPUT_ROUTED = "input:routed"                         # 输入已分发到目标（target/capability/archived）
```

- [ ] **Step 2: 收录进 ALL_EVENTS**

在 `ALL_EVENTS = frozenset({...})` 内追加 `INPUT_CLASSIFIED, INPUT_QUEUED, INPUT_ROUTED,`。

- [ ] **Step 3: 运行事件 schema 测试**

Run: `python -m pytest tests/test_events_schema.py -q`
Expected: PASS（唯一性/命名/ALL_EVENTS 一致性）

- [ ] **Step 4: 提交**

```bash
git add src/shared/events.py
git commit -m "feat(events): input 域事件（input:classified/queued/routed）"
```

---

### Task 2: InputClassifier（五类识别 + 优先级标签）

**Files:**
- Create: `src/commander/input/input_classifier.py`
- Test: `tests/test_input_classifier.py`

- [ ] **Step 1: 写失败测试**

```python
"""test_input_classifier.py — 输入分类（五类 + 优先级 + 身份/深度标记）"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.commander.input.input_classifier import (
    InputClassifier, InputEnvelope, InputType,
)


def _c(text="", source="", event="", **kw):
    return InputClassifier().classify(text=text, source=source, event=event, **kw)


def test_operator_command_source():
    e = _c(text="你好", source="command")
    assert e.input_type == InputType.OPERATOR
    assert e.priority == 0
    assert e.operator_id == "user"


def test_operator_bang_prefix():
    e = _c(text="!点歌 晴天", source="danmaku")
    assert e.input_type == InputType.OPERATOR
    assert e.operator_id == "user"


def test_audience_danmaku():
    e = _c(text="主播好", source="danmaku")
    assert e.input_type == InputType.AUDIENCE
    assert e.priority == 1


def test_audience_gift_event():
    e = _c(source="gift", event="gift:received")
    assert e.input_type == InputType.AUDIENCE


def test_external_app_screen():
    e = _c(source="screen", event="screen:cursor_action")
    assert e.input_type == InputType.EXTERNAL_APP
    assert e.priority == 2


def test_system_loop_source():
    e = _c(source="system_loop", loop_depth=1)
    assert e.input_type == InputType.SYSTEM_LOOP
    assert e.priority == 3
    assert e.loop_depth == 1


def test_reference_worldbook():
    e = _c(text="查询 Yuki 的设定", source="command", kind="reference")
    assert e.input_type == InputType.REFERENCE
    assert e.priority == -1  # 不排队


def test_fallback_audience():
    e = _c(text="未知来源内容", source="unknown_thing")
    assert e.input_type == InputType.AUDIENCE
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_input_classifier.py -q`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 input_classifier.py**

```python
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

# 观众域事件前缀 → audience
_AUDIENCE_EVENTS = ("danmaku", "gift", "guard", "superchat", "audience", "interact")
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

        # audience：观众事件前缀或其余来源
        return InputEnvelope(InputType.AUDIENCE, PRIORITY[InputType.AUDIENCE],
                             source, {"text": text, "event": event, **kw})
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_input_classifier.py -q`
Expected: PASS（9 passed）

- [ ] **Step 5: 提交**

```bash
git add src/commander/input/input_classifier.py tests/test_input_classifier.py
git commit -m "feat(input): InputClassifier 五类识别 + 优先级标签"
```

---

### Task 3: PriorityQueue（排序 + 插队 + 循环深度上限）

**Files:**
- Create: `src/commander/input/priority_queue.py`
- Test: `tests/test_priority_queue.py`

- [ ] **Step 1: 写失败测试**

```python
"""test_priority_queue.py — 输入优先级队列"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.commander.input.input_classifier import InputClassifier, InputType
from src.commander.input.priority_queue import PriorityQueue


def _env(t, **kw):
    return InputClassifier().classify(source=t.value, loop_depth=kw.get("loop_depth", 0), **kw)


def test_push_pop_by_priority():
    q = PriorityQueue()
    q.push(_env(InputType.AUDIENCE))
    q.push(_env(InputType.EXTERNAL_APP))
    q.push(_env(InputType.SYSTEM_LOOP, loop_depth=1))
    assert q.pop().input_type == InputType.EXTERNAL_APP   # P2 先出
    assert q.pop().input_type == InputType.AUDIENCE       # P1
    assert q.pop().input_type == InputType.SYSTEM_LOOP    # P3


def test_same_priority_fifo():
    q = PriorityQueue()
    q.push(_env(InputType.AUDIENCE, text="a"))
    q.push(_env(InputType.AUDIENCE, text="b"))
    assert q.pop().payload["text"] == "a"
    assert q.pop().payload["text"] == "b"


def test_operator_insert_front():
    q = PriorityQueue()
    q.push(_env(InputType.AUDIENCE, text="弹幕"))
    q.insert_front(_env(InputType.OPERATOR, text="!点歌"))
    assert q.pop().payload["text"] == "!点歌"


def test_loop_depth_exceeded_rejected():
    q = PriorityQueue(max_loop_depth=5)
    assert q.push(_env(InputType.SYSTEM_LOOP, loop_depth=5)) is True   # 等于上限可入
    assert q.push(_env(InputType.SYSTEM_LOOP, loop_depth=6)) is False  # 超限拒绝


def test_operator_bypasses_depth_limit():
    q = PriorityQueue(max_loop_depth=5)
    assert q.insert_front(_env(InputType.OPERATOR, loop_depth=99)) is True
    assert q.size() == 1


def test_empty_pop_returns_none():
    q = PriorityQueue()
    assert q.pop() is None
    assert q.size() == 0
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_priority_queue.py -q`
Expected: FAIL

- [ ] **Step 3: 实现 priority_queue.py**

```python
"""priority_queue.py — 输入优先级队列（总控调度化，规格 2026-08-22 任务一）

按优先级排序（P0>P1>P2>P3，同优先级 FIFO）；operator 可直插队首；
系统循环携带深度标记，超过上限拒绝入队（归档短期记忆由调用方决定）。

# 模块内容清单 — priority_queue
## 1. 模块身份标识
- 所属调度官：commander（input 域）
- 能力名：无（指挥官内部服务）
## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| max_loop_depth | 否 | 5 | int>=1 | 系统自循环深度上限 |
## 3. 输入契约
- push(envelope) -> bool；insert_front(envelope) -> bool；pop() -> Optional[InputEnvelope]
## 4. 输出契约
- 成功：push/insert_front 返回 True；pop 返回队首（无则 None）
- 失败：深度超限返回 False（拒绝入队）
## 5. 依赖声明
- 外部服务：无
- 内部模块：threading、typing、input_classifier.InputEnvelope/InputType
## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 深度超限 | system_loop.loop_depth > max_loop_depth | push 返回 False，调用方归档 |
## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | - | 线程安全（RLock），随调随用 |
## 8. 领域状态说明
- 状态项：_queues（按优先级分桶的 deque）
- 持久化：无
"""
import threading
from collections import deque
from typing import Any, Dict, List, Optional

from src.commander.input.input_classifier import InputEnvelope, InputType, PRIORITY

_ORDERED_TYPES = [InputType.OPERATOR, InputType.EXTERNAL_APP,
                  InputType.AUDIENCE, InputType.SYSTEM_LOOP]  # P0→P3


class PriorityQueue:
    """按优先级排序的输入队列（operator 插队、循环深度上限）。"""

    def __init__(self, max_loop_depth: int = 5):
        self._max_loop_depth = max_loop_depth
        self._buckets: Dict[str, deque] = {t.value: deque() for t in _ORDERED_TYPES}
        self._lock = threading.RLock()

    def push(self, envelope: InputEnvelope) -> bool:
        """入队；system_loop 超深度上限返回 False（拒绝）。"""
        if envelope.input_type == InputType.SYSTEM_LOOP and envelope.loop_depth > self._max_loop_depth:
            return False
        with self._lock:
            self._buckets[envelope.input_type].append(envelope)
        return True

    def insert_front(self, envelope: InputEnvelope) -> bool:
        """operator 直插队首（绕过队列排序，跳过深度限制）。"""
        if envelope.input_type == InputType.OPERATOR:
            with self._lock:
                self._buckets[InputType.OPERATOR.value].appendleft(envelope)
            return True
        return False

    def pop(self) -> Optional[InputEnvelope]:
        """取最高优先级队首（同优先级 FIFO）。空队列返回 None。"""
        with self._lock:
            for t in _ORDERED_TYPES:
                bucket = self._buckets[t.value]
                if bucket:
                    return bucket.popleft()
        return None

    def size(self) -> int:
        with self._lock:
            return sum(len(b) for b in self._buckets.values())

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return {t.value: len(self._buckets[t.value]) for t in _ORDERED_TYPES}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_priority_queue.py -q`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add src/commander/input/priority_queue.py tests/test_priority_queue.py
git commit -m "feat(input): PriorityQueue 优先级排序/插队/循环深度上限"
```

---

### Task 4: DistributionRouter（按类型路由）

**Files:**
- Create: `src/commander/input/distribution_router.py`
- Test: `tests/test_distribution_router.py`

- [ ] **Step 1: 写失败测试**

```python
"""test_distribution_router.py — 输入分发路由"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.commander.input.input_classifier import InputClassifier, InputType
from src.commander.input.distribution_router import DistributionRouter


class FakeParser:
    """意图解析桩：仅 system_loop 文本命中 '捡装备' 视为意图。"""

    def parse(self, text, source="danmaku", session_id="default"):
        from src.commander.intent_parser import Command
        if "捡装备" in (text or ""):
            return Command(capability="game:op_command", payload={"text": text},
                           source=source, session_id=session_id)
        return Command(capability="llm:chat", payload={"text": text},
                       source=source, session_id=session_id)


class FakeRouter:
    def __init__(self):
        self.calls = []

    async def dispatch(self, cmd):
        self.calls.append(cmd.capability)
        return {"ok": True, "data": {"reply": "ok"}}


class FakePipeline:
    def __init__(self):
        self.calls = []

    async def execute_with(self, text, role="yuki", **kw):
        self.calls.append(text)
        return {"ok": True}


def _router(parser=None, cmd_router=None, pipeline=None):
    return DistributionRouter(intent_parser=parser or FakeParser(),
                              command_router=cmd_router or FakeRouter(),
                              danmaku_pipeline=pipeline or FakePipeline())


def test_operator_routes_to_command_router():
    r = _router()
    env = InputClassifier().classify(text="!点歌 晴天", source="command")
    result = asyncio.run(r.route(env))
    assert result["target"] == "command_router"


def test_system_loop_intent_routes_to_command_router():
    r = _router()
    env = InputClassifier().classify(text="我去捡装备了", source="system_loop", loop_depth=1)
    result = asyncio.run(r.route(env))
    assert result["target"] == "command_router"
    assert result["capability"] == "game:op_command"
    assert result["archived"] is False


def test_system_loop_no_intent_archived():
    r = _router()
    env = InputClassifier().classify(text="今天天气不错", source="system_loop", loop_depth=1)
    result = asyncio.run(r.route(env))
    assert result["target"] == "archive"
    assert result["archived"] is True


def test_audience_routes_to_pipeline():
    r = _router()
    env = InputClassifier().classify(text="主播好", source="danmaku")
    result = asyncio.run(r.route(env))
    assert result["target"] == "danmaku_pipeline"
    assert r._pipeline.calls == ["主播好"]


def test_reference_not_routed():
    r = _router()
    env = InputClassifier().classify(text="查设定", source="command", kind="reference")
    result = asyncio.run(r.route(env))
    assert result["target"] == "context"  # 参考资料走上下文聚合，不响应
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_distribution_router.py -q`
Expected: FAIL

- [ ] **Step 3: 实现 distribution_router.py**

```python
"""distribution_router.py — 输入分发路由（总控调度化，规格 2026-08-22 任务一）

按输入类型 + 意图命中路由到目标：
- operator / system_loop(命中意图) → command_router.dispatch（意图解析）
- audience → danmaku_pipeline.execute_with
- system_loop(未命中意图) → archive（归档短期记忆，不触发响应）
- external_app / reference → 不在此分发（外部应用透传事件；reference 走上下文）

# 模块内容清单 — distribution_router
## 1. 模块身份标识
- 所属调度官：commander（input 域）
- 能力名：无（指挥官内部服务）
## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| 无 | - | - | - | 依赖注入 intent_parser/command_router/danmaku_pipeline/event_bus |
## 3. 输入契约
- route(envelope: InputEnvelope) -> dict
## 4. 输出契约
- 成功：{"ok", "target", "capability", "archived"}；archive 时 archived=True
- 失败：目标未注入 → {"ok": False, "target": "archive", "archived": True}
## 5. 依赖声明
- 外部服务：无
- 内部模块：intent_parser、input_classifier、shared.events（可选）
## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 目标未注入 | command_router/pipeline 为 None | 归档返回，不抛异常 |
## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | - | 无状态（持有注入引用） |
## 8. 领域状态说明
- 状态项：无
- 持久化：无
"""
import asyncio
import logging
from typing import Any, Dict, Optional

from src.commander.input.input_classifier import InputEnvelope, InputType

logger = logging.getLogger(__name__)


class DistributionRouter:
    """按类型与意图分发输入。"""

    def __init__(self, intent_parser=None, command_router=None,
                 danmaku_pipeline=None, event_bus=None):
        self._parser = intent_parser
        self._cmd_router = command_router
        self._pipeline = danmaku_pipeline
        self._event_bus = event_bus

    async def route(self, envelope: InputEnvelope) -> Dict[str, Any]:
        t = envelope.input_type
        if t == InputType.REFERENCE:
            return {"ok": True, "target": "context", "capability": "",
                    "archived": False}
        if t == InputType.EXTERNAL_APP:
            # 外部应用事件透传（由对应域订阅消费），此处仅记录
            return {"ok": True, "target": "event_bus", "capability": "",
                    "archived": False}
        if t == InputType.AUDIENCE:
            if self._pipeline is None:
                return {"ok": False, "target": "archive", "capability": "",
                        "archived": True}
            text = envelope.payload.get("text", "")
            await self._pipeline.execute_with(text, role=envelope.meta.get("role", "yuki"))
            return {"ok": True, "target": "danmaku_pipeline",
                    "capability": "llm:chat", "archived": False}
        # operator / system_loop：意图解析 → 命中分发，未命中归档
        if self._parser is None or self._cmd_router is None:
            return {"ok": False, "target": "archive", "capability": "",
                    "archived": True}
        text = envelope.payload.get("text", "")
        cmd = self._parser.parse(text, source=envelope.source,
                                 session_id="default")
        if t == InputType.SYSTEM_LOOP and cmd.capability == "llm:chat":
            # 系统自循环无明确意图 → 归档短期记忆，不触发新一轮分发
            return {"ok": True, "target": "archive", "capability": "llm:chat",
                    "archived": True}
        result = await self._cmd_router.dispatch(cmd)
        if self._event_bus is not None:
            from src.shared.events import INPUT_ROUTED
            self._event_bus.publish(INPUT_ROUTED, target="command_router",
                                    capability=cmd.capability, archived=False,
                                    input_type=t.value)
        return {"ok": bool(result.get("ok")), "target": "command_router",
                "capability": cmd.capability, "archived": False}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_distribution_router.py -q`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add src/commander/input/distribution_router.py tests/test_distribution_router.py
git commit -m "feat(input): DistributionRouter 类型/意图路由（未命中意图归档）"
```

---

### Task 5: ContextAggregator（上下文快照）

**Files:**
- Create: `src/commander/input/context_aggregator.py`
- Test: `tests/test_context_aggregator.py`

- [ ] **Step 1: 写失败测试**

```python
"""test_context_aggregator.py — 上下文聚合"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.commander.input.context_aggregator import ContextAggregator


class FakeMemory:
    async def handle(self, command):
        if command["capability"] == "memory:get_history":
            return {"ok": True, "data": {"history": [
                {"role": "user", "content": "刚说过的话"},
                {"role": "assistant", "content": "回复内容"}]}}
        return {"ok": True, "data": {"memories": []}}


class FakeSession:
    role = "yuki"
    scene = "chat"
    live_mode = "offline"

    def snapshot(self):
        return {"role": self.role, "scene": self.scene, "live_mode": self.live_mode}


def _agg(memory=None, session=None):
    return ContextAggregator(memory=memory or FakeMemory(), session=session or FakeSession())


def test_build_merges_memory_session_and_reference():
    import asyncio
    agg = _agg()
    ctx = asyncio.run(agg.build(role="yuki"))
    assert "history" in ctx and len(ctx["history"]) == 2
    assert ctx["session"]["role"] == "yuki"
    assert ctx["reference"] == []  # reference 资料槽位（世界书/脚本由调用方填充）
    assert "snapshot_ts" in ctx


def test_build_without_memory_safe():
    import asyncio
    agg = ContextAggregator(memory=None, session=None)
    ctx = asyncio.run(agg.build())
    assert ctx["history"] == []
    assert ctx["session"] == {}
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_context_aggregator.py -q`
Expected: FAIL

- [ ] **Step 3: 实现 context_aggregator.py**

```python
"""context_aggregator.py — 上下文聚合（总控调度化，规格 2026-08-22 任务一）

合并短期记忆 + reference 资料（世界书/脚本等）+ 会话状态 → 上下文快照。
供 LLM 调用方与批量问询（任务二）使用；reference 槽位由调用方填充（不排队）。

# 模块内容清单 — context_aggregator
## 1. 模块身份标识
- 所属调度官：commander（input 域）
- 能力名：无（指挥官内部服务）
## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| 无 | - | - | - | 依赖注入 memory/session/event_bus |
## 3. 输入契约
- build(role="", reference=None, max_history=20) -> dict
## 4. 输出契约
- 成功：{"history", "session", "reference", "snapshot_ts"}
- 失败：无异常路径（缺失依赖返回空段）
## 5. 依赖声明
- 外部服务：无
- 内部模块：memory_orchestrator（可选）、session_context（可选）、shared.events（可选）
## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 无 | - | 依赖缺失时对应段为空 |
## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | - | 无状态 |
## 8. 领域状态说明
- 状态项：无
- 持久化：无
"""
import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ContextAggregator:
    """短期记忆 + reference + 会话状态 → 上下文快照。"""

    def __init__(self, memory=None, session=None, event_bus=None):
        self._memory = memory
        self._session = session
        self._event_bus = event_bus

    async def build(self, role: str = "", reference: Optional[List[Dict]] = None,
                    max_history: int = 20) -> Dict[str, Any]:
        history: List[Dict[str, str]] = []
        if self._memory is not None:
            try:
                payload = {"session_id": "default", "limit": max_history}
                if role:
                    payload["character_id"] = role
                result = await self._memory.handle({
                    "capability": "memory:get_history", "payload": payload})
                history = result.get("data", {}).get("history", []) or []
            except Exception as e:
                logger.warning("[ContextAggregator] 记忆读取失败: %s", e)
        session_snap = {}
        if self._session is not None:
            try:
                session_snap = self._session.snapshot()
            except Exception as e:
                logger.warning("[ContextAggregator] 会话快照失败: %s", e)
        ctx = {"history": history,
               "session": session_snap,
               "reference": reference or [],
               "snapshot_ts": time.time()}
        if self._event_bus is not None:
            try:
                from src.shared.events import CONTEXT_SNAPSHOT_READY
                self._event_bus.publish(CONTEXT_SNAPSHOT_READY, context=ctx)
            except Exception as e:
                logger.debug("[ContextAggregator] 快照事件发布失败: %s", e)
        return ctx
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_context_aggregator.py -q`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
git add src/commander/input/context_aggregator.py tests/test_context_aggregator.py
git commit -m "feat(input): ContextAggregator 上下文快照聚合"
```

---

### Task 6: input 域 __init__ 导出 + 装配（app.py）

**Files:**
- Create: `src/commander/input/__init__.py`
- Modify: `src/app.py`（装配 input 域 + 开关注册）

- [ ] **Step 1: 写 __init__.py**

```python
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
| input.dispatch_mode | 否 | direct | direct/priority/adaptive | 总控分发模式（config.yaml） |
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
from src.commander.input.input_classifier import InputClassifier, InputEnvelope, InputType
from src.commander.input.priority_queue import PriorityQueue
from src.commander.input.distribution_router import DistributionRouter
from src.commander.input.context_aggregator import ContextAggregator

__all__ = ["InputClassifier", "InputEnvelope", "InputType",
           "PriorityQueue", "DistributionRouter", "ContextAggregator"]
```

- [ ] **Step 2: app.py 装配（在指挥官段后、pipeline 之前）**

在 `command_router = CommandRouter(...)` 之后追加：

```python
    # ---------- input 域（总控调度化，规格 2026-08-22 任务一） ----------
    from src.commander.input import (
        InputClassifier, PriorityQueue, DistributionRouter, ContextAggregator)
    input_classifier = InputClassifier()
    input_queue = PriorityQueue()
    distribution_router = DistributionRouter(
        intent_parser=intent_parser, command_router=command_router,
        danmaku_pipeline=pipeline, event_bus=event_bus)
    context_aggregator = ContextAggregator(
        memory=memory_orch, session=session, event_bus=event_bus)
    # 总控分发模式开关：默认关（direct 直通，现有链路不变）；开 = priority 排队
    switch_manager.auto_register("input_dispatch", default=False)
```

> 注：`pipeline` 需在 `distribution_router` 之前创建（当前 app.py 顺序为 pipeline 在 router 之后），
> 因此将 input 装配放到 pipeline.start() 之后，或在 router 构造时不传 pipeline（None 亦可，
> audience 直通由原 danmaku_pipeline 订阅处理）。**P1 采用后者**：`danmaku_pipeline=pipeline`
> 的注入放到 pipeline 创建之后单独赋值：
> `distribution_router._pipeline = pipeline`（若 route 已调用则无需，P1 入口未接线 route 主链）。

- [ ] **Step 3: 运行全量测试**

Run: `python -m pytest tests -q`
Expected: 全绿（input 域模块不影响现有链路）

- [ ] **Step 4: 提交**

```bash
git add src/commander/input/__init__.py src/app.py
git commit -m "feat(input): input 域装配 + input_dispatch 开关（默认直通）"
```

---

### Task 7: operator 入口接线（command.py 发布 input:classified）

**Files:**
- Modify: `src/web/routes/command.py`

- [ ] **Step 1: 修改 command()，operator 输入发布分类事件（行为不变）**

在 `cmd = parser.parse(...)` 之前插入：

```python
    # input 域：operator 输入分类（总控调度化，P1 直通接线——行为不变，仅发布事件）
    try:
        from src.commander.input import InputClassifier
        from src.shared.events import INPUT_CLASSIFIED
        env = InputClassifier().classify(text=text, source="command", kind="operator")
        event_bus = context.get("event_bus")
        if event_bus is not None:
            event_bus.publish(INPUT_CLASSIFIED, input_type=env.input_type,
                              priority=env.priority, source=env.source,
                              operator_id=env.operator_id, loop_depth=env.loop_depth)
    except Exception as e:
        logger.debug("[command] input 分类事件发布失败: %s", e)
```

- [ ] **Step 2: 新增测试（test_web_routes.py）**

```python
def test_command_publishes_input_classified():
    """总控调度化：POST /api/command 发布 input:classified（operator 身份标记）。"""
    from src.shared.events import INPUT_CLASSIFIED
    ctx = _make_context()
    app = create_app(ctx)
    seen = {}
    ctx["event_bus"].subscribe(INPUT_CLASSIFIED, lambda event, **kw: seen.update(kw))
    client = app.test_client()
    resp = client.post("/api/command", json={"text": "你好"})
    assert resp.status_code == 200
    assert seen.get("input_type") == "operator"
    assert seen.get("operator_id") == "user"
```

- [ ] **Step 3: 运行测试**

Run: `python -m pytest tests/test_web_routes.py -q`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add src/web/routes/command.py tests/test_web_routes.py
git commit -m "feat(input): operator 命令入口发布 input:classified（直通接线）"
```

---

### Task 8: 全量验证

- [ ] **Step 1: 全量 pytest**

Run: `python -m pytest tests -q`
Expected: 全绿

- [ ] **Step 2: 冒烟 L0**

Run: `python scripts/smoke_test.py --check-env`
Expected: 全 PASS

- [ ] **Step 3: 提交（若有遗漏变更）**

```bash
git add -A
git commit -m "test(input): 任务一全量验证"
```

**任务一出口条件：** input 域 4 模块 + 事件 + 开关 + command 接线全部落地，新增测试全绿，现有 561+ 测试不回归。
