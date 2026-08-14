# Future Scene V2 前端重构实施计划（方案 A · 全量快照流）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重做系统前端，实现状态唯一性（全局 seq）、数据唯一性（后到覆盖先到）、前端只发事件不预测数值、助手与前端数据一致。

**Architecture:** 后端 EventBus 发布时分配全局单调 seq；状态变更发布 `state:changed` 全量快照（含 version）；前端单一 store 按双游标（lastSnapshotSeq / lastEventSeq）合并事件与快照；dashboard/assistant 重写，subtitle/live2d 轻接入。

**Tech Stack:** Python 3.10 + Flask + flask_sock（后端）；原生 JS + 轻量状态层 store.js（前端，无构建步骤）。

**Spec:** `docs/superpowers/specs/2026-08-14-frontend-rework-design.md`

---

## 文件结构

```
src/shared/event_bus.py            修改：EventRecord 加 seq，publish 锁内分配，暴露 current_seq()
src/shared/events.py               修改：新增 STATE_CHANGED
src/web/state_provider.py          新建：快照聚合 provider（version/session/switches/orchestrators/degradation/cost/watchdog）
src/web/routes/ws.py               修改：广播消息携带 seq
src/web/routes/state.py            修改：返回统一快照 + version
src/web/routes/command.py          修改：返回 command_id
src/web/routes/switch.py           修改：返回 command_id
src/commander/intent_parser.py     修改：Command 加 command_id 字段
src/commander/command_router.py    修改：生成并透传 command_id
src/app.py                         修改：装配 StatePublisher，订阅五类触发事件
src/commander/state_publisher.py   新建：订阅状态变更事件 → 发布 state:changed
frontend/assets/store.js           新建：前端单一 store（seq 合并 / 双游标 / 命令状态机 / 断线重连）
frontend/assets/state_sync.js      新建：WS 客户端 + 快照拉取 + 断线补数（供四页面共用）
frontend/dashboard/index.html      重写：store 驱动全部面板
frontend/assistant/index.html      重写状态部分：store 驱动状态面板
frontend/subtitle_overlay/index.html  轻接入：数据源改 store 订阅 frontend:*
frontend/live2d_stream/index.html  轻接入：数据源改 store 订阅 live2d:*/audio:*
tests/test_event_bus.py            修改：seq 单调性测试
tests/test_state_provider.py       新建：快照结构 + version 测试
tests/test_command_router.py       修改：command_id 透传测试
tests/test_web_routes.py           修改：version/command_id 断言
```

---

# 组 A：后端协议层（Task 1-8）

### Task 1: EventBus 全局 seq

**Files:**
- Modify: `src/shared/event_bus.py`
- Test: `tests/test_event_bus.py`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_event_bus.py`）

```python
def test_seq_monotonic():
    bus = EventBus()
    bus.reset()
    bus.subscribe("session:switched", lambda **kw: None)
    bus.subscribe("llm:requested", lambda **kw: None)
    bus.publish("session:switched", role="yuki")
    bus.publish("llm:requested", text="hi")
    history = bus.get_history(limit=10)
    seqs = [r.seq for r in history if r.seq]
    assert len(seqs) == 2
    assert seqs[0] < seqs[1]
    assert bus.current_seq() == seqs[1]


def test_seq_assigned_without_subscribers():
    bus = EventBus()
    bus.reset()
    bus.publish("session:switched", role="yuki")
    assert bus.current_seq() >= 1
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_event_bus.py::test_seq_monotonic -v`
Expected: FAIL（AttributeError: 'EventRecord' object has no attribute 'seq'）

- [ ] **Step 3: 实现 seq**

在 `src/shared/event_bus.py` 中：

```python
# EventRecord dataclass 增加字段（第 65-71 行附近）
@dataclass
class EventRecord:
    event: str
    data: Dict
    handler_count: int
    timestamp: float = 0.0
    seq: int = 0  # 全局单调序号（v1.2 新增）
    handler_results: List[str] = field(default_factory=list)

# __init__ 中增加计数器（第 104 行附近，_initialized = True 之前）
        self._seq_counter = 0

# publish() 中分配（第 168 行，record 创建处）
        with self._mutex:
            self._seq_counter += 1
            record = EventRecord(event=event, data=data,
                                 handler_count=len(handlers),
                                 seq=self._seq_counter)
```

注意：`publish()` 当前在 `_resolve_handlers` 之后创建 record。需将 record 创建与 seq 分配放入 `with self._mutex` 块内，确保原子性。修改后 publish 的 record 创建部分：

```python
        handlers = self._resolve_handlers(event)
        if not handlers:
            # 无订阅者也要分配 seq（保持全局单调，供快照 version 使用）
            with self._mutex:
                self._seq_counter += 1
            return
        self._track_fuse(event)
        with self._mutex:
            self._seq_counter += 1
            record = EventRecord(event=event, data=data,
                                 handler_count=len(handlers),
                                 seq=self._seq_counter)
```

并新增方法：

```python
    def current_seq(self) -> int:
        """当前全局事件序号（快照 version 读取用）。"""
        with self._mutex:
            return self._seq_counter
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_event_bus.py -v`
Expected: PASS（含既有测试）

- [ ] **Step 5: 提交**

```bash
git add src/shared/event_bus.py tests/test_event_bus.py
git commit -m "feat(event-bus): 全局单调 seq 分配与 current_seq()"
```

> 注：项目当前非 git 仓库，提交步骤可跳过或先 `git init`。后续提交步骤同样适用此说明。

---

### Task 2: 新增 state:changed 事件 + state_provider 快照聚合

**Files:**
- Modify: `src/shared/events.py`
- Create: `src/web/state_provider.py`
- Test: `tests/test_state_provider.py`

- [ ] **Step 1: events.py 新增事件**

在 `src/shared/events.py` 运维域（`COST_CIRCUIT_OPEN` 附近）追加：

```python
STATE_CHANGED = "state:changed"                       # 状态快照推送（含 version，供前端全量更新）
```

- [ ] **Step 2: 写失败测试**（新建 `tests/test_state_provider.py`）

```python
"""state_provider 测试：快照聚合 + version。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.shared.event_bus import EventBus
from src.web.state_provider import StateProvider


class FakeSession:
    def snapshot(self):
        return {"session_id": "default", "role": "yuki"}


class FakeSwitchManager:
    def snapshot(self):
        return {"llm": True, "tts": False}


class FakeRegistry:
    def all(self):
        return [type("O", (), {"name": "llm"}), type("O", (), {"name": "tts"})]


class FakeDegradation:
    def snapshot(self):
        return {"llm": "normal"}


class FakeMetrics:
    def __call__(self):
        return {"cost": {"total_cost": 0.1}, "watchdog": {"llm": "ok"}}


def test_snapshot_structure():
    bus = EventBus()
    provider = StateProvider(
        event_bus=bus,
        session=FakeSession(),
        switch_manager=FakeSwitchManager(),
        registry=FakeRegistry(),
        degradation_manager=FakeDegradation(),
        metrics_provider=FakeMetrics(),
    )
    snap = provider.snapshot()
    assert set(snap.keys()) == {"version", "session", "switches",
                                "orchestrators", "degradation", "cost", "watchdog"}
    assert snap["session"]["role"] == "yuki"
    assert snap["switches"]["tts"] is False
    assert snap["orchestrators"] == ["llm", "tts"]
    assert isinstance(snap["version"], int)


def test_version_equals_current_seq():
    bus = EventBus()
    provider = StateProvider(event_bus=bus, session=FakeSession(),
                             switch_manager=FakeSwitchManager(),
                             registry=FakeRegistry(),
                             degradation_manager=FakeDegradation(),
                             metrics_provider=FakeMetrics())
    snap = provider.snapshot()
    assert snap["version"] == bus.current_seq()
```

- [ ] **Step 3: 运行确认失败**

Run: `python -m pytest tests/test_state_provider.py -v`
Expected: FAIL（ModuleNotFoundError: No module named 'src.web.state_provider'）

- [ ] **Step 4: 实现 StateProvider**（新建 `src/web/state_provider.py`）

```python
"""state_provider.py — 系统状态快照聚合（前端重构 · 方案 A）

统一生成 {version, session, switches, orchestrators, degradation, cost, watchdog}，
供 /api/state、/api/metrics 与 state:changed 事件共用，保证全链路同一份快照。

# 模块内容清单（8 项契约）
1. 模块身份标识：web · StateProvider · 对外 snapshot()
2. 配置契约：构造注入 event_bus/session/switch_manager/registry/degradation_manager/metrics_provider
3. 输入契约：snapshot() 无参数
4. 输出契约：{version, session, switches, orchestrators, degradation, cost, watchdog}；version=event_bus.current_seq()
5. 依赖声明：typing、src.shared.event_bus
6. 错误定义：组件缺失时对应字段返回空（{} / []）
7. 生命周期方法：snapshot()（无状态）
8. 领域状态说明：无模块级可变状态
"""
from typing import Any, Dict, Optional


class StateProvider:
    """系统状态快照聚合器（唯一快照来源）。"""

    def __init__(self, event_bus, session=None, switch_manager=None,
                 registry=None, degradation_manager=None,
                 metrics_provider=None):
        self._event_bus = event_bus
        self._session = session
        self._switch_manager = switch_manager
        self._registry = registry
        self._degradation = degradation_manager
        self._metrics = metrics_provider

    def snapshot(self) -> Dict[str, Any]:
        version = self._event_bus.current_seq() if self._event_bus else 0
        metrics = self._metrics() if self._metrics else {}
        return {
            "version": version,
            "session": self._session.snapshot() if self._session else {},
            "switches": self._switch_manager.snapshot() if self._switch_manager else {},
            "orchestrators": [o.name for o in self._registry.all()]
            if self._registry else [],
            "degradation": self._degradation.snapshot() if self._degradation else {},
            "cost": metrics.get("cost", {}),
            "watchdog": metrics.get("watchdog", {}),
        }
```

- [ ] **Step 5: 运行确认通过**

Run: `python -m pytest tests/test_state_provider.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add src/shared/events.py src/web/state_provider.py tests/test_state_provider.py
git commit -m "feat(state): 新增 state:changed 事件与快照聚合 provider"
```

---

### Task 3: command_id 生成与透传

**Files:**
- Modify: `src/commander/intent_parser.py`
- Modify: `src/commander/command_router.py`
- Test: `tests/test_command_router.py`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_command_router.py`）

```python
def test_dispatch_publishes_command_id():
    bus = EventBus()
    bus.reset()
    from src.commander.intent_parser import Command
    received = []
    bus.subscribe("commander:command_received", lambda **kw: received.append(kw))
    bus.subscribe("commander:command_completed", lambda **kw: received.append(kw))
    orch = FakeOrch()  # 既有测试中的 stub，capability="llm:chat"
    registry = FakeRegistryWith([orch])
    sm = FakeSwitchManager(True)
    router = CommandRouter(registry, sm, bus)
    cmd = Command(capability="llm:chat", payload={}, source="command",
                  session_id="default")
    import asyncio
    result = asyncio.run(router.dispatch(cmd))
    assert result["ok"] is True
    cids = [kw.get("command_id") for kw in received if kw.get("command_id")]
    assert len(cids) == 2
    assert cids[0] == cids[1]
    assert len(cids[0]) == 32  # uuid4().hex


def test_existing_command_id_preserved():
    bus = EventBus()
    bus.reset()
    from src.commander.intent_parser import Command
    seen = {}
    bus.subscribe("commander:command_completed", lambda **kw: seen.update(kw))
    orch = FakeOrch()
    registry = FakeRegistryWith([orch])
    router = CommandRouter(registry, FakeSwitchManager(True), bus)
    cmd = Command(capability="llm:chat", payload={}, source="command",
                  session_id="default", command_id="predefined-123")
    import asyncio
    asyncio.run(router.dispatch(cmd))
    assert seen.get("command_id") == "predefined-123"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_command_router.py::test_dispatch_publishes_command_id -v`
Expected: FAIL（TypeError: Command.__init__() got an unexpected keyword argument 'command_id'）

- [ ] **Step 3: Command 加字段**（`src/commander/intent_parser.py` 第 28-35 行）

```python
@dataclass
class Command:
    """结构化命令（规格书 4.4）。"""
    capability: str  # 能力名，如 "llm:chat"
    payload: Dict[str, Any]  # 结构化参数
    source: str  # danmaku / command / voice / system
    session_id: str  # 会话 ID
    raw: str = ""  # 原始文本（审计/调试）
    command_id: str = ""  # 命令追踪 ID（路由层生成，事件透传）
```

- [ ] **Step 4: CommandRouter 生成并透传**（`src/commander/command_router.py`）

```python
import uuid  # 文件顶部

    async def dispatch(self, command: Command) -> Dict[str, Any]:
        if not command.command_id:
            command.command_id = uuid.uuid4().hex
        cid = command.command_id
        self._event_bus.publish(COMMAND_RECEIVED, command=command,
                                command_id=cid)

        # D4 纪律：调用前必须检查开关
        orch = self._registry.match(command.capability)
        if orch is None:
            return {"ok": False, "error": f"unknown capability: {command.capability}",
                    "command_id": cid}
        if not self._switch_manager.is_enabled(orch.name):
            return {"ok": False, "error": f"orchestrator disabled: {orch.name}",
                    "command_id": cid}

        self._event_bus.publish(COMMAND_ROUTED, capability=command.capability,
                                target=orch.name, command_id=cid)
        try:
            result = await orch.handle({"capability": command.capability,
                                        "payload": command.payload,
                                        "command_id": cid})
            result.setdefault("command_id", cid)
            self._event_bus.publish(COMMAND_COMPLETED, capability=command.capability,
                                    result=result, command_id=cid)
            return result
        except Exception as e:
            logger.error("[CommandRouter] 调度官 %s 执行异常: %s", orch.name, e)
            self._event_bus.publish(COMMAND_FAILED, capability=command.capability,
                                    error=str(e), command_id=cid)
            return {"ok": False, "error": str(e), "command_id": cid}
```

- [ ] **Step 5: 运行确认通过**

Run: `python -m pytest tests/test_command_router.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add src/commander/intent_parser.py src/commander/command_router.py tests/test_command_router.py
git commit -m "feat(command): command_id 生成与事件透传"
```

---

### Task 4: WS 广播携带 seq

**Files:**
- Modify: `src/web/routes/ws.py`
- Test: `tests/test_web_routes.py`

- [ ] **Step 1: 修改 `_broadcast` 携带 seq**

`src/web/routes/ws.py` 中 `_broadcast` 改为从 EventBus 当前 seq 取序号：

```python
def _broadcast(event: str, **data) -> None:
    """EventBus 回调：广播事件给所有 WS 客户端（消息携带 seq）。"""
    if not _clients:
        return
    try:
        seq = _seq_provider() if _seq_provider else 0
        message = json.dumps({"type": event, "seq": seq, **data},
                             ensure_ascii=False, default=str)
    except (TypeError, ValueError) as e:
        logger.warning("[WS] 事件序列化失败: %s", e)
        return
    dead = []
    for client in list(_clients):
        try:
            client.send(message)
        except Exception:
            dead.append(client)
    for client in dead:
        _clients.discard(client)
```

`init_ws` 签名增加 `seq_provider`：

```python
def init_ws(app, event_bus, seq_provider=None) -> None:
    """注册 WS 路由并订阅 EventBus 广播。

    seq_provider: 可选 Callable[[], int]，返回 EventBus 当前 seq。
    """
    global _seq_provider
    _seq_provider = seq_provider
    ...
```

模块顶部增加：

```python
_seq_provider = None
```

- [ ] **Step 2: app_factory 传入 seq_provider**

`src/web/app_factory.py` 第 50 行改为：

```python
    ws_route.init_ws(app, context.get("event_bus"),
                     seq_provider=lambda: context.get("event_bus").current_seq()
                     if context.get("event_bus") else 0)
```

- [ ] **Step 3: 运行既有测试确认不破坏**

Run: `python -m pytest tests/test_web_routes.py tests/test_p2_app_boot.py -v`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add src/web/routes/ws.py src/web/app_factory.py
git commit -m "feat(ws): 广播消息携带全局 seq"
```

---

### Task 5: /api/state 与 /api/metrics 返回统一快照 + version

**Files:**
- Modify: `src/web/routes/state.py`
- Modify: `src/web/app_factory.py`
- Test: `tests/test_web_routes.py`

- [ ] **Step 1: 修改 state 路由**

`src/web/routes/state.py` 的 `state()` 改为使用 StateProvider：

```python
from src.web.state_provider import StateProvider

@bp.route("/state", methods=["GET"])
def state():
    context = current_app.config.get("APP_CONTEXT", {})
    provider = context.get("state_provider")
    if provider is None:
        # 回退：无 provider 时返回旧字段结构（兼容未装配场景）
        session = context.get("session")
        switch_manager = context.get("switch_manager")
        registry = context.get("registry")
        return jsonify({
            "version": context.get("event_bus").current_seq()
            if context.get("event_bus") else 0,
            "session": session.snapshot() if session else {},
            "switches": switch_manager.snapshot() if switch_manager else {},
            "orchestrators": [o.name for o in registry.all()] if registry else [],
            "degradation": context.get("degradation_manager").snapshot()
            if context.get("degradation_manager") else {},
            "cost": {}, "watchdog": {},
        })
    return jsonify(provider.snapshot())
```

- [ ] **Step 2: 修改 metrics 路由**

`src/web/app_factory.py` 的 `metrics()` 改为统一快照：

```python
    @app.get("/api/metrics")
    def metrics():
        provider = context.get("state_provider")
        if provider is None:
            return jsonify({"cost": {}, "watchdog": {}, "version": 0,
                            "circuit_breaker": {}})
        snap = provider.snapshot()
        return jsonify({"cost": snap["cost"], "watchdog": snap["watchdog"],
                        "version": snap["version"],
                        "circuit_breaker": context.get("cost_breaker").snapshot()
                        if context.get("cost_breaker") else {}})
```

- [ ] **Step 3: 写测试断言 version**（追加到 `tests/test_web_routes.py`）

```python
def test_state_returns_version():
    # 用既有 test app fixture（若无则建最小 Flask app + context）
    app, _ = build_test_app()  # 既有 helper
    client = app.test_client()
    resp = client.get("/api/state")
    data = resp.get_json()
    assert "version" in data
    assert isinstance(data["version"], int)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_web_routes.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/web/routes/state.py src/web/app_factory.py tests/test_web_routes.py
git commit -m "feat(api): state/metrics 返回统一快照与 version"
```

---

### Task 6: StatePublisher 订阅五类触发事件

**Files:**
- Create: `src/commander/state_publisher.py`
- Modify: `src/app.py`
- Test: `tests/test_state_provider.py`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_state_provider.py`）

```python
def test_state_publisher_publishes_on_trigger():
    bus = EventBus()
    bus.reset()
    from src.commander.state_publisher import StatePublisher
    provider = StateProvider(event_bus=bus, session=FakeSession(),
                             switch_manager=FakeSwitchManager(),
                             registry=FakeRegistry(),
                             degradation_manager=FakeDegradation(),
                             metrics_provider=FakeMetrics())
    publisher = StatePublisher(bus, provider)
    publisher.start()
    snapshots = []
    bus.subscribe("state:changed", lambda **kw: snapshots.append(kw))
    # 触发开关变更
    bus.publish("switch:changed", name="llm", enabled=False)
    assert len(snapshots) == 1
    assert "snapshot" in snapshots[0]
    assert snapshots[0]["snapshot"]["version"] <= bus.current_seq()
    # 非触发事件不发布
    bus.publish("llm:requested", text="hi")
    assert len(snapshots) == 1
    publisher.stop()
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_state_provider.py::test_state_publisher_publishes_on_trigger -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 StatePublisher**（新建 `src/commander/state_publisher.py`）

```python
"""state_publisher.py — 状态变更 → state:changed 快照发布（前端重构 · 方案 A）

订阅五类触发事件，收到后生成全量快照并发布 state:changed：
1. switch:changed（开关切换）
2. session:switched / session:state_changed（角色/会话变更）
3. degradation 变更（降级管理器）
4. watchdog 状态翻转（ok↔degraded↔down）
5. cost:circuit_open（成本熔断触发）

# 模块内容清单（8 项契约）
1. 模块身份标识：commander · StatePublisher · 对外 start()/stop()
2. 配置契约：构造注入 event_bus/state_provider
3. 输入契约：订阅事件回调（event, **data）
4. 输出契约：发布 STATE_CHANGED 事件，data={"snapshot": 全量快照}
5. 依赖声明：logging、src.shared.event_bus、src.shared.events
6. 错误定义：快照生成异常记录日志不中断
7. 生命周期方法：start() 订阅 / stop() 取消订阅
8. 领域状态说明：_handlers 记录订阅回调引用
"""
import logging

from src.shared.events import (
    COST_CIRCUIT_OPEN,
    SESSION_STATE_CHANGED,
    SESSION_SWITCHED,
    STATE_CHANGED,
    SWITCH_CHANGED,
)

logger = logging.getLogger(__name__)

# 五类触发事件（含通配符降级/看门狗域）
_TRIGGER_EVENTS = [
    SWITCH_CHANGED,
    SESSION_SWITCHED,
    SESSION_STATE_CHANGED,
    "degradation:*",
    "watchdog:*",
    COST_CIRCUIT_OPEN,
]


class StatePublisher:
    """状态变更 → state:changed 全量快照发布器。"""

    def __init__(self, event_bus, state_provider):
        self._event_bus = event_bus
        self._provider = state_provider
        self._handlers = {}

    def start(self) -> None:
        for evt in _TRIGGER_EVENTS:
            self._handlers[evt] = lambda event=evt, **kw: self._on_change(event, **kw)
            try:
                self._event_bus.subscribe(evt, self._handlers[evt], name=f"StatePublisher:{evt}")
            except ValueError:
                logger.warning("[StatePublisher] 事件 %s 未注册，跳过", evt)
        logger.info("[StatePublisher] 已启动，订阅 %d 类触发事件", len(_TRIGGER_EVENTS))

    def stop(self) -> None:
        for evt, handler in self._handlers.items():
            self._event_bus.unsubscribe(evt, handler)
        self._handlers.clear()

    def _on_change(self, event: str, **data) -> None:
        try:
            snapshot = self._provider.snapshot()
        except Exception as e:
            logger.error("[StatePublisher] 快照生成失败: %s", e)
            return
        self._event_bus.publish(STATE_CHANGED, snapshot=snapshot, trigger=event)
```

- [ ] **Step 4: app.py 装配 StatePublisher**

`src/app.py` 在 `metrics_provider` 定义后、`context` 组装前加入：

```python
    from src.commander.state_publisher import StatePublisher
    from src.web.state_provider import StateProvider

    state_provider = StateProvider(
        event_bus=event_bus,
        session=session,
        switch_manager=switch_manager,
        registry=registry,
        degradation_manager=degradation,
        metrics_provider=metrics_provider,
    )
    state_publisher = StatePublisher(event_bus, state_provider)
    state_publisher.start()
```

并在 `context` 字典增加：

```python
        "state_provider": state_provider,
        "state_publisher": state_publisher,
```

- [ ] **Step 5: 运行确认通过 + 全量回归**

Run: `python -m pytest tests/test_state_provider.py tests/test_p2_app_boot.py -v`
Expected: PASS

Run: `python -m pytest -q`
Expected: 全部通过（原 253 + 新增）

- [ ] **Step 6: 提交**

```bash
git add src/commander/state_publisher.py src/app.py tests/test_state_provider.py
git commit -m "feat(state): StatePublisher 订阅五类触发事件发布 state:changed"
```

---

### Task 7: 看门狗状态翻转去抖

**Files:**
- Modify: `src/shared/watchdog.py`
- Test: `tests/test_watchdog.py`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_watchdog.py`）

```python
def test_check_publishes_only_on_flip():
    from src.shared.event_bus import EventBus
    bus = EventBus()
    events = []
    bus.subscribe("watchdog:changed", lambda **kw: events.append(kw))
    wd = Watchdog()
    wd._event_bus = bus  # 注入（Watchdog 构造无 event_bus 参数，用属性注入）
    calls = {"n": 0}

    def health():
        calls["n"] += 1
        return {"status": "ok"}

    wd.register("llm", health)
    wd.check()
    wd.check()
    assert len(events) == 0  # 连续 ok 不触发

    # 翻转到 down
    def health_down():
        raise RuntimeError("boom")
    wd.register("llm", health_down)
    wd.check()
    assert len(events) == 1
    assert events[0]["name"] == "llm"
    assert events[0]["status"] == "down"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_watchdog.py::test_check_publishes_only_on_flip -v`
Expected: FAIL（订阅 watchdog:changed 时 ValueError: 未注册事件名）

- [ ] **Step 3: events.py 新增 watchdog:changed**

在 `src/shared/events.py` 运维域追加：

```python
WATCHDOG_CHANGED = "watchdog:changed"                 # 看门狗健康状态翻转（ok↔degraded↔down）
```

- [ ] **Step 4: Watchdog 状态翻转发布**

`src/shared/watchdog.py`：

```python
# __init__ 增加
        self._event_bus = None  # 可选注入，发布 watchdog:changed

    def bind_event_bus(self, event_bus) -> None:
        self._event_bus = event_bus

# check() 中状态变化时发布（在 `with self._lock` 更新 _status 前比较旧值）
        with self._lock:
            old = self._status.get(name, {}).get("status", "unknown")
            self._status[name] = {"status": status, "last_check": time.time(),
                                  "detail": detail}
            changed = old != status
        if changed and self._event_bus is not None:
            try:
                from src.shared.events import WATCHDOG_CHANGED
                self._event_bus.publish(WATCHDOG_CHANGED, name=name, status=status)
            except Exception as e:
                logger.warning("[Watchdog] 发布状态变更事件失败: %s", e)
```

- [ ] **Step 5: app.py 绑定 event_bus**

`src/app.py` watchdog 创建后：

```python
    watchdog = Watchdog()
    watchdog.bind_event_bus(event_bus)
```

- [ ] **Step 6: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_watchdog.py -v`
Expected: PASS

Run: `python -m pytest -q`
Expected: 全部通过

- [ ] **Step 7: 提交**

```bash
git add src/shared/events.py src/shared/watchdog.py src/app.py tests/test_watchdog.py
git commit -m "feat(watchdog): 健康状态翻转发布 watchdog:changed"
```

---

### Task 8: 成本跨阈值触发 + 助手 prompt 实时性审查

**Files:**
- Modify: `src/commander/cost_tracker.py`
- Modify: `src/commander/cost_circuit_breaker.py`
- Modify: `src/commander/session_context.py`
- Test: `tests/test_cost_circuit_breaker.py`

- [ ] **Step 1: 成本跨阈值发布 cost:milestone**

在 `src/shared/events.py` 运维域追加：

```python
COST_MILESTONE = "cost:milestone"                     # 成本累计每满 1.00 元发布（触发 state:changed）
```

`src/commander/cost_tracker.py` 的 `record()` 中，`_daily_cost` 更新后检查跨整元：

```python
    def record(self, call_type: str, model: str = "", prompt_tokens: int = 0,
               completion_tokens: int = 0, chars: int = 0) -> float:
        # ... 现有成本计算 ...
        cost = ...  # 现有逻辑
        self._total_cost += cost
        before = int(self._total_cost)
        after = int(self._total_cost)
        if after > before and self._event_bus is not None:
            try:
                from src.shared.events import COST_MILESTONE
                self._event_bus.publish(COST_MILESTONE, total_cost=self._total_cost)
            except Exception:
                pass
        return cost
```

注：`cost_tracker.py` 当前 `record` 需先确认是否持有 `_event_bus` 引用，若无则在 `__init__` 增加参数 `event_bus=None`，`src/app.py` 创建时传入 `CostTracker(event_bus=event_bus)`。

- [ ] **Step 2: 写测试**（追加 `tests/test_cost_circuit_breaker.py` 或新建）

```python
def test_cost_milestone_publishes():
    from src.shared.event_bus import EventBus
    from src.commander.cost_tracker import CostTracker
    bus = EventBus()
    events = []
    bus.subscribe("cost:milestone", lambda **kw: events.append(kw))
    tracker = CostTracker(event_bus=bus)
    # 手动设 total 到 0.95 再 +0.10 跨 1.00
    tracker._total_cost = 0.95
    tracker.record(call_type="llm", model="deepseek-v4-pro",
                   prompt_tokens=100, completion_tokens=100)
    assert len(events) >= 1
    assert events[-1]["total_cost"] >= 1.0
```

- [ ] **Step 3: 助手 prompt 实时性审查**

审查 `src/commander/session_context.py` 与 LLM prompt 组装路径，确认角色/开关读取直接访问最新值。若发现缓存副本，移除。本次验收方式：

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_cost_circuit_breaker.py -v`
Expected: PASS

Run: `python -m pytest -q`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add src/shared/events.py src/commander/cost_tracker.py src/commander/cost_circuit_breaker.py tests/test_cost_circuit_breaker.py
git commit -m "feat(cost): 成本跨整元发布 cost:milestone"
```

---

# 组 B：前端状态层与页面（Task 9-14）

### Task 9: 前端 store.js（单一 store + 双游标合并）

**Files:**
- Create: `frontend/assets/store.js`
- Create: `frontend/assets/state_sync.js`

- [ ] **Step 1: 实现 store.js**

```javascript
/* ============================================
   store.js — Future Scene 前端单一状态层（方案 A）
   状态唯一性：以后端 EventBus seq 为准，后到覆盖先到。
   双游标：lastSnapshotSeq（快照）/ lastEventSeq（事件）独立合并。
   ============================================ */
(function (global) {
  'use strict';

  function createStore(reducer, initialState) {
    let state = initialState;
    const listeners = []; // {filter, fn}

    function getState() { return state; }

    function dispatch(event) {
      const prev = state;
      state = reducer(state, event);
      if (state !== prev) {
        listeners.forEach(function (l) {
          if (l.filter === '*' || (event.type || '').indexOf(l.filter) === 0) {
            l.fn(state, event, prev);
          }
        });
      }
      return state;
    }

    function subscribe(filter, fn) {
      const l = { filter: filter || '*', fn: fn };
      listeners.push(l);
      return function () {
        const i = listeners.indexOf(l);
        if (i >= 0) listeners.splice(i, 1);
      };
    }

    return { getState: getState, dispatch: dispatch, subscribe: subscribe };
  }

  /* ---- reducer：双游标合并 ---- */
  function makeReducer() {
    return function (state, event) {
      const type = event.type || '';
      const seq = event.seq || 0;
      const next = JSON.parse(JSON.stringify(state));

      if (type === 'state:changed' || (event.snapshot && event.version !== undefined)) {
        // 快照：仅当 version > lastSnapshotSeq 接受
        const version = event.version !== undefined ? event.version : (event.snapshot && event.snapshot.version);
        if (version === undefined || version > next.snapshotSeq) {
          const snap = event.snapshot || event;
          next.snapshotSeq = version !== undefined ? version : next.snapshotSeq;
          next.session = snap.session || next.session;
          next.switches = snap.switches || next.switches;
          next.orchestrators = snap.orchestrators || next.orchestrators;
          next.degradation = snap.degradation || next.degradation;
          next.cost = snap.cost || next.cost;
          next.watchdog = snap.watchdog || next.watchdog;
          next.version = version !== undefined ? version : next.version;
        }
        return next;
      }

      // 普通事件：仅当 seq > lastEventSeq 接受（事件日志）
      if (seq <= next.seq) return state; // 过期事件丢弃，不产生新引用
      next.seq = seq;
      next.events = next.events.concat([{
        type: type, seq: seq, ts: event.ts || Date.now(), data: event
      }]).slice(-200);

      // 命令状态机
      if (event.command_id) {
        const cmds = next.commands;
        if (type === 'commander:command_received') {
          cmds[event.command_id] = { status: 'sent', raw: '', error: null };
        } else if (type === 'commander:command_routed') {
          if (cmds[event.command_id]) cmds[event.command_id].status = 'running';
        } else if (type === 'commander:command_completed') {
          if (cmds[event.command_id]) cmds[event.command_id].status = 'success';
        } else if (type === 'commander:command_failed') {
          if (cmds[event.command_id]) {
            cmds[event.command_id].status = 'failed';
            cmds[event.command_id].error = event.error || 'unknown';
          }
        }
      }

      // 会话/开关增量（无快照时的兜底）
      if (type === 'switch:changed' && event.name !== undefined) {
        next.switches = Object.assign({}, next.switches, { [event.name]: event.enabled });
      }
      if (type === 'session:switched' && event.role) {
        next.session = Object.assign({}, next.session, { role: event.role });
      }
      return next;
    };
  }

  /* ---- 初始状态 ---- */
  function initialState() {
    return {
      seq: 0,            // lastEventSeq
      snapshotSeq: -1,   // lastSnapshotSeq
      version: 0,
      session: {}, switches: {}, orchestrators: [], degradation: {},
      cost: {}, watchdog: {},
      events: [],
      commands: {}
    };
  }

  global.FSStore = {
    createStore: createStore,
    makeReducer: makeReducer,
    initialState: initialState
  };
})(window);
```

- [ ] **Step 2: 实现 state_sync.js（WS 客户端 + 断线补数）**

```javascript
/* ============================================
   state_sync.js — WS 客户端 + 快照初始化 + 断线补数
   初始化顺序：先拉 /api/state（带 version）→ 再建 WS。
   断线：立即拉一次 /api/state → 指数退避重连 → 重连成功再拉一次。
   ============================================ */
(function (global) {
  'use strict';

  function StateSync(store, opts) {
    opts = opts || {};
    this.store = store;
    this.ws = null;
    this.reconnectDelay = opts.reconnectDelay || 1000;
    this.maxDelay = opts.maxDelay || 30000;
    this.pollInterval = opts.pollInterval || 30000;
    this._pollTimer = null;
    this._manualStop = false;
  }

  StateSync.prototype.init = function () {
    var self = this;
    return fetch('/api/state').then(function (r) { return r.json(); }).then(function (data) {
      if (data && data.version !== undefined) {
        self.store.dispatch({ type: 'state:changed', snapshot: data, version: data.version });
      }
      self.connect();
    }).catch(function () {
      // 首拉失败仍尝试 WS（后端可能只装配了部分）
      self.connect();
    });
  };

  StateSync.prototype.connect = function () {
    var self = this;
    if (this._manualStop) return;
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    try {
      this.ws = new WebSocket(proto + location.host + '/ws/events');
    } catch (e) {
      this._scheduleReconnect();
      return;
    }
    this.ws.onmessage = function (evt) {
      var msg;
      try { msg = JSON.parse(evt.data); } catch (e) { return; }
      self.store.dispatch(msg);
    };
    this.ws.onclose = function () {
      self._onDisconnect();
    };
    this.ws.onerror = function () {
      try { self.ws.close(); } catch (e) {}
    };
    this.ws.onopen = function () {
      self.reconnectDelay = 1000;
      self._stopPolling();
      // 重连成功：拉一次快照补缺口（双游标自动防回退）
      fetch('/api/state').then(function (r) { return r.json(); }).then(function (data) {
        if (data && data.version !== undefined) {
          self.store.dispatch({ type: 'state:changed', snapshot: data, version: data.version });
        }
      }).catch(function () {});
    };
  };

  StateSync.prototype._onDisconnect = function () {
    var self = this;
    // 断线立即拉一次（兜底可见性）
    fetch('/api/state').then(function (r) { return r.json(); }).then(function (data) {
      if (data && data.version !== undefined) {
        self.store.dispatch({ type: 'state:changed', snapshot: data, version: data.version });
      }
    }).catch(function () {});
    this._startPolling();
    this._scheduleReconnect();
  };

  StateSync.prototype._scheduleReconnect = function () {
    var self = this;
    setTimeout(function () { self.connect(); }, this.reconnectDelay);
    this.reconnectDelay = Math.min(this.reconnectDelay * 2, this.maxDelay);
  };

  StateSync.prototype._startPolling = function () {
    var self = this;
    this._stopPolling();
    this._pollTimer = setInterval(function () {
      fetch('/api/state').then(function (r) { return r.json(); }).then(function (data) {
        if (data && data.version !== undefined) {
          self.store.dispatch({ type: 'state:changed', snapshot: data, version: data.version });
        }
      }).catch(function () {});
    }, this.pollInterval);
  };

  StateSync.prototype._stopPolling = function () {
    if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; }
  };

  StateSync.prototype.stop = function () {
    this._manualStop = true;
    this._stopPolling();
    if (this.ws) { try { this.ws.close(); } catch (e) {} }
  };

  global.FSStateSync = StateSync;
})(window);
```

- [ ] **Step 3: 验证 JS 语法**

Run: `node --check frontend/assets/store.js; node --check frontend/assets/state_sync.js`
Expected: 无输出（语法通过）；若 node 不存在则跳过，用浏览器手动验证

- [ ] **Step 4: 提交**

```bash
git add frontend/assets/store.js frontend/assets/state_sync.js
git commit -m "feat(frontend): 单一 store + WS 同步（双游标合并）"
```

---

### Task 10: dashboard 重写（store 驱动）

**Files:**
- Modify: `frontend/dashboard/index.html`

- [ ] **Step 1: 引入 store + state_sync**

`<head>` 中 tokens.css 之后引入：

```html
<link rel="stylesheet" href="/frontend/assets/tokens.css">
<script src="/frontend/assets/store.js"></script>
<script src="/frontend/assets/state_sync.js"></script>
```

初始化：

```javascript
var store = FSStore.createStore(FSStore.makeReducer(), FSStore.initialState());
var sync = new FSStateSync(store);
sync.init();

// 事件日志渲染
store.subscribe('*', function (state, event) {
  if (event.type === 'state:changed') { renderAll(state); return; }
  appendEventLog(event);
});

// 开关渲染（仅快照到达后更新）
store.subscribe('switch:', function (state, event) {
  if (event.type === 'switch:changed') { renderSwitches(state.switches); }
});
```

- [ ] **Step 2: 开关操作改为"发事件不预测"**

原 `toggleSwitch(name, enable)` 改为：

```javascript
function toggleSwitch(name, enable) {
  fetch('/api/switch/' + encodeURIComponent(name), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled: enable })
  }).then(function (r) { return r.json(); }).then(function (data) {
    if (!data.ok) { toast('开关操作失败: ' + (data.error || ''), 'error'); return; }
    if (data.command_id) {
      // 记录命令状态，等待 state:changed 到达后 UI 才更新（不预测）
      var cmds = store.getState().commands;
      cmds[data.command_id] = { status: 'sent', raw: name, error: null };
    }
    // 不直接改 UI！等待 state:changed 事件
  }).catch(function () { toast('网络错误', 'error'); });
}
```

- [ ] **Step 3: 移除 5s 轮询**

删除 `setInterval(loadState, 5000)`、`setInterval(loadHealth, 5000)`、`setInterval(loadMetrics, 5000)`，仅保留首屏一次 `fetch('/api/state')`（由 state_sync.init 承担）与事件驱动更新。

- [ ] **Step 4: renderAll 从 store 渲染**

将 `renderSession/renderSwitches/renderDegradation/renderRegistry/renderKpiOrch/renderLiveBadge` 统一改为读 `store.getState()`，由 `renderAll(state)` 一次调用。

- [ ] **Step 5: 浏览器验证**

Run: 启动后端 `python src/app.py`，访问 `http://localhost:5000/dashboard/`
Expected: 面板显示真实数据；切换开关后 UI 在 `state:changed` 到达后更新；事件日志实时滚动

- [ ] **Step 6: 提交**

```bash
git add frontend/dashboard/index.html
git commit -m "feat(dashboard): store 驱动重写，开关操作不预测"
```

---

### Task 11: assistant 重写状态部分

**Files:**
- Modify: `frontend/assistant/index.html`

- [ ] **Step 1: 引入 store + state_sync**

```html
<script src="/frontend/assets/store.js"></script>
<script src="/frontend/assets/state_sync.js"></script>
```

- [ ] **Step 2: 状态面板走 store**

替换现有 `refreshState/refreshMetrics` 轮询（`setInterval(verifyStateSync, 30000)` 等）：

```javascript
var store = FSStore.createStore(FSStore.makeReducer(), FSStore.initialState());
var sync = new FSStateSync(store);
sync.init();

store.subscribe('state:changed', function (state) {
  renderStatusPanel(state);   // 角色/开关/成本/看门狗
});
store.subscribe('commander:', function (state, event) {
  updateCommandStatus(event); // 命令状态机与对话消息关联
});
store.subscribe('llm:', function (state, event) {
  if (event.type === 'llm:stream_chunk') appendStreamChunk(event);
  if (event.type === 'llm:responded') finalizeReply(event);
});
```

- [ ] **Step 3: 对话历史保持 localStorage**

保留 `HISTORY_KEY` 读写逻辑不变（单标签可见）。刷新时恢复对话历史 + 从 `sessionStorage` 恢复状态面板（若已存）。

- [ ] **Step 4: 发送指令带 command_id 关联**

```javascript
function sendCommand(raw) {
  API.post('/api/command', { text: raw, session_id: SESSION_ID }, TIMEOUT_COMMAND).then(function (data) {
    if (data.command_id) {
      store.getState().commands[data.command_id] = { status: 'sent', raw: raw, error: null };
    }
    if (!data.ok) {
      addMessage('system', '命令失败: ' + (data.error || ''), 'error');
      return;
    }
    // 不预测回复！等待 llm:responded / command_completed 事件
  }).catch(function (e) {
    addMessage('system', '网络错误', 'error');
  });
}
```

- [ ] **Step 5: 语音输入保留**

保留 Web Speech API 逻辑（`SpeechRecognition`），识别结果填入输入框后走 `sendCommand`。

- [ ] **Step 6: 浏览器验证**

Run: 访问 `http://localhost:5000/assistant/`
Expected: 状态面板显示真实数据；"切换到 Lilith"后角色在 `state:changed` 到达时更新；对话区显示结果；语音可用

- [ ] **Step 7: 提交**

```bash
git add frontend/assistant/index.html
git commit -m "feat(assistant): 状态面板 store 化，命令带 command_id 追踪"
```

---

### Task 12: subtitle_overlay 轻接入

**Files:**
- Modify: `frontend/subtitle_overlay/index.html`

- [ ] **Step 1: 引入 store + 订阅 frontend:/tts:**

```html
<script src="/frontend/assets/store.js"></script>
<script src="/frontend/assets/state_sync.js"></script>
<script>
  var store = FSStore.createStore(FSStore.makeReducer(), FSStore.initialState());
  var sync = new FSStateSync(store);
  sync.init();
  // 只订阅字幕相关事件，忽略其他状态变更
  store.subscribe('frontend:', function (state, event) {
    if (event.type === 'frontend:subtitle_update') renderSubtitle(event);
  });
  store.subscribe('tts:', function (state, event) {
    if (event.type === 'tts:audio_ready') maybePlayAudio(event);
  });
</script>
```

- [ ] **Step 2: 移除演示模式**

删除"后端未就绪时模拟字幕"的演示分支（`TODO: 确认` 标注处），数据源统一 store。

- [ ] **Step 3: 浏览器验证**

Run: 访问 `http://localhost:5000/subtitle/`
Expected: 后端推字幕时显示，无后端时不显示（无假数据）

- [ ] **Step 4: 提交**

```bash
git add frontend/subtitle_overlay/index.html
git commit -m "feat(subtitle): 数据源改 store，移除演示假字幕"
```

---

### Task 13: live2d_stream 轻接入

**Files:**
- Modify: `frontend/live2d_stream/index.html`

- [ ] **Step 1: 引入 store + 订阅 live2d:/audio:**

```html
<script src="/frontend/assets/store.js"></script>
<script src="/frontend/assets/state_sync.js"></script>
<script>
  var store = FSStore.createStore(FSStore.makeReducer(), FSStore.initialState());
  var sync = new FSStateSync(store);
  sync.init();
  store.subscribe('live2d:', function (state, event) {
    if (event.type === 'live2d:motion_triggered') triggerMotion(event.motion);
    if (event.type === 'live2d:expression_changed') setExpression(event.expression);
  });
  store.subscribe('audio:', function (state, event) {
    if (event.type === 'audio:segment_ready') playSegment(event.audio_id);
  });
</script>
```

- [ ] **Step 2: 浏览器验证**

Run: 访问 `http://localhost:5000/live2d/`
Expected: Live2D 加载正常，口型/表情由事件驱动

- [ ] **Step 3: 提交**

```bash
git add frontend/live2d_stream/index.html
git commit -m "feat(live2d): 数据源改 store 订阅 live2d:/audio:"
```

---

### Task 14: 刷新恢复（sessionStorage）+ 收尾

**Files:**
- Modify: `frontend/assets/store.js`
- Modify: `frontend/dashboard/index.html`
- Modify: `frontend/assistant/index.html`

- [ ] **Step 1: store 持久化辅助**

`store.js` 增加：

```javascript
  function persistOnUnload(store, key) {
    window.addEventListener('pagehide', function () {
      try {
        var s = store.getState();
        sessionStorage.setItem(key, JSON.stringify({
          session: s.session, switches: s.switches, cost: s.cost,
          watchdog: s.watchdog, degradation: s.degradation,
          snapshotSeq: s.snapshotSeq
        }));
      } catch (e) {}
    });
  }

  function restoreFromSession(store, key) {
    try {
      var raw = sessionStorage.getItem(key);
      if (!raw) return false;
      var saved = JSON.parse(raw);
      store.dispatch({ type: 'state:changed', snapshot: saved, version: saved.snapshotSeq });
      return true;
    } catch (e) { return false; }
  }

  global.FSStore.persistOnUnload = persistOnUnload;
  global.FSStore.restoreFromSession = restoreFromSession;
```

- [ ] **Step 2: dashboard/assistant 接入持久化**

dashboard 初始化：

```javascript
FSStore.restoreFromSession(store, 'fs-dash-state');
FSStore.persistOnUnload(store, 'fs-dash-state');
```

assistant 初始化：

```javascript
FSStore.restoreFromSession(store, 'fs-assistant-state');
FSStore.persistOnUnload(store, 'fs-assistant-state');
```

- [ ] **Step 3: 全量回归**

Run: `python -m pytest -q`
Expected: 全部通过

Run: 手动四页面验收（对照设计文档第 8 节验收清单）
- [ ] 开关切换 UI 不立即变化，`state:changed` 到达后更新
- [ ] 乱序事件按 seq 合并，旧事件不覆盖新状态
- [ ] 加载中收到 WS 事件，快照仍正确回填（双游标）
- [ ] 切换角色后立即对话，prompt 角色为最新
- [ ] 四页面功能正常
- [ ] 断线重连状态一致

- [ ] **Step 4: 提交**

```bash
git add frontend/assets/store.js frontend/dashboard/index.html frontend/assistant/index.html
git commit -m "feat(frontend): 刷新恢复 sessionStorage + 验收通过"
```

---

# 验收清单（最终）

1. 全部 pytest 通过（原 253 + 新增 seq/state_provider/command_id/watchdog/cost 测试）。
2. 四页面手动验收 6 项全过。
3. 前端无轮询滞后：状态变更在 `state:changed` 到达即更新。
4. 前端无预测：开关/命令操作后 UI 等待事件确认。
5. 助手 prompt 实时性：角色切换后对话立即反映。
