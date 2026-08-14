# Future Scene V2 前端重构设计文档（方案 A · 全量快照流）

日期：2026-08-14
状态：已批准（用户确认全部决策点后进入实施）

## 1. 背景与目标

旧系统前端与后端脱节：前端可能显示假数据、后端无法响应前端操作、多通道（HTTP 轮询 + WS 事件）交错导致状态乱序。

重构目标：
1. **状态唯一性**：全系统只允许一套数据，以后端 EventBus 为唯一数据源。
2. **数据唯一性**：以事件到达顺序为准，后到覆盖先到（通过全局 seq 强制）。
3. **前端不预测**：前端操作只发事件，事件到达并实施成功后才显示结果。
4. **功能不退化**：全部现有功能（开关、命令、角色切换、成本、看门狗、事件日志）在重构后正常。
5. **助手一致性**：智能助手与前端数据一致；助手改动实时传到前端；助手 prompt 组装时实时拉取状态（不缓存）。

## 2. 关键决策（用户已确认）

| 决策点 | 结论 |
|---|---|
| 重构边界 | 前后端一起改（API / WS / EventBus） |
| 技术栈 | 原生 JS + 轻量状态层（无构建步骤） |
| 整体方案 | 方案 A：全量快照流 + 全局 seq |
| 页面范围 | 两重两轻：dashboard + assistant 重写，subtitle + live2d 轻接入 |
| 多标签页 | 不支持；对话历史 localStorage 单标签可见，未来可 BroadcastChannel |
| 助手实时性 | prompt 组装时直接从 SessionContext + SwitchManager 取最新值，不经缓存 |

## 3. 后端协议层改动

### 3.1 EventBus 全局 seq（P0 核心）

- `EventRecord` 增加 `seq: int` 字段。
- `EventBus` 增加单调计数器，`publish()` 在互斥锁内分配 `seq`，与广播同刻完成。
- 对外暴露 `current_seq()` 供快照 provider 读取当前序号。
- WS 广播消息统一为 `{"type": <event>, "seq": <int>, "ts": <float>, **data}`。
- `get_history()` 返回的记录含 seq。

链路原子性：状态变更 → 业务构造快照 → `publish("state:changed", snapshot)` → 锁内分配 seq → 广播。快照内容与 seq 同刻生成，无不同步窗口。

### 3.2 `state:changed` 事件 + 快照 provider（P0）

- `events.py` 新增 `STATE_CHANGED = "state:changed"`。
- 新增 `src/web/state_provider.py`：聚合快照 = `{version, session, switches, orchestrators, degradation, cost, watchdog}`，由现有组件生成。
  - `version` = 调用瞬间的 `event_bus.current_seq()`（锁内读取）。
- `/api/state`、`/api/metrics` 改为由同一 provider 生成，响应携带 `version`。
- 触发条件（仅以下五类，避免微小变更风暴）：
  1. 开关切换（`switch:changed`）
  2. 角色切换（`session:switched` / `session:state_changed`）
  3. 降级管理器变更
  4. 看门狗 health 状态翻转（ok↔degraded↔down，连续相同不触发）
  5. 成本跨阈值（每累计满 1.00 元或熔断触发）
- 实现方式：在 `src/app.py` 装配一个 `StatePublisher`，订阅上述事件，收到后重新生成快照并 `publish(STATE_CHANGED, snapshot=...)`。注意防重复：若 `switch:changed` 与 `session:switched` 同时发生，两次都发布（前端按 seq 合并，后到覆盖）。

### 3.3 `command_id` 追踪（P0）

- `Command` 数据类新增 `command_id: str = ""` 字段（`src/commander/intent_parser.py`）。
- `CommandRouter.dispatch()` 中：`command_id` 为空时生成 `uuid4().hex`，并透传给调度官 `handle()`。
- 调度官发布事件时携带 `command_id`（从 command.payload 或 handle 参数读取）。
- `POST /api/command`、`POST /api/switch` 响应返回 `command_id`。
- `COMMAND_RECEIVED / COMMAND_ROUTED / COMMAND_COMPLETED / COMMAND_FAILED` 事件均携带 `command_id`。

### 3.4 快照双游标协议（P0，前端侧规则）

前端维护两个独立游标：
- `lastSnapshotSeq`：最近接受的快照 seq（初始 -1）。
- `lastEventSeq`：最近接受的事件 seq（初始 -1）。

接受规则：
- 快照（`state:changed` 或 HTTP `/api/state`）：`version > lastSnapshotSeq` 才接受，接受后 `lastSnapshotSeq = version`。
- 普通事件：`seq > lastEventSeq` 才接受，接受后 `lastEventSeq = seq`。

该设计解决"拉快照时 seq=100、WS 事件 seq=101 已到达，快照被误丢弃"的竞态：普通事件不会阻止快照回填，只有更新快照才拒绝旧快照。

### 3.5 助手实时性（P0）

- 审查 `commander` 组装 LLM prompt 的代码路径，确认系统状态（角色/开关/成本）直接读 `SessionContext` / `SwitchManager` 最新值。
- 若存在缓存副本或快照缓存，移除。
- 验收方式：切换角色后立即发聊天指令，prompt 中注入的角色为最新值。

## 4. 前端状态层（新增 `frontend/assets/store.js`）

### 4.1 store API

```js
createStore(reducer, initialState) → {
  getState(), dispatch(event), subscribe(filter, fn), getLastSeq(), getLastSnapshotSeq()
}
```

- `subscribe(filter, fn)`：filter 为事件名前缀或 `"*"`（全部）。字幕/Live2D 页只订阅所需前缀，减少无关状态变更。

### 4.2 reducer 结构

```js
state = {
  seq: 0,            // lastEventSeq
  snapshotSeq: -1,   // lastSnapshotSeq
  session: {}, switches: {}, orchestrators: [], degradation: {},
  cost: {}, watchdog: {},
  events: [],        // 最近 200 条 {type, seq, ts, data}
  commands: {}       // command_id → {status: sent|running|success|failed, raw, error}
}
```

### 4.3 初始化与重连

- 首载：先 `GET /api/state`（带 version）初始化 store → 再建立 WS。
- WS 断线：指数退避重连；断线瞬间立即触发一次 HTTP `/api/state` 拉取（兜底）；重连成功后拉一次 `/api/state` 补缺口。
- 重连成功到快照到达之间的事件照常按 seq 接受（普通事件与快照独立游标）。

### 4.4 命令状态机（UI 反馈）

- 每条命令维护 `{sent → running → success | failed}`：
  - 发送命令拿到 `command_id` → 记录 `sent`。
  - 收到 `command_routed` → `running`。
  - 收到 `command_completed` → `success`；`command_failed` → `failed`（显示 error）。
- 与对话消息关联展示，前端不预测结果。

### 4.5 刷新恢复（sessionStorage）

- `pagehide`/`beforeunload` 时把 store 状态（不含对话历史）序列化到 `sessionStorage`。
- 加载时若存在则恢复为初始状态（仍按 seq 接受后续事件，可被事件流覆盖）。
- 对话历史仍由 localStorage 单独管理（单标签）。

## 5. 页面迁移

### 5.1 dashboard（重写）

- 全部面板由 store 驱动；去掉 5s 轮询 `/api/state`、`/api/metrics`（保留一次首屏拉取 + 断线兜底）。
- 开关操作：`POST /api/switch/{name}` → 返回后**不直接改 UI**，等待 `state:changed` 到达才更新开关状态（不预测）。
- 命令操作：`POST /api/command` → 显示 command_id 状态机。
- 事件日志：按 seq 追加，断线补回后按 seq 排序合并。

### 5.2 assistant（重写状态部分）

- 状态面板（角色/开关/成本/看门狗）走 store，去掉 15/30/60s 轮询。
- 对话历史仍 localStorage（单标签）。
- 发命令后等 `state:changed`/`command_completed` 事件确认再更新界面。
- 语音输入、切换角色、查看状态等现有功能全部保留。

### 5.3 subtitle_overlay（轻接入）

- 引入 store 但只 `subscribe("frontend:", fn)`、`subscribe("tts:", fn)`。
- 渲染逻辑与视觉结构不动，只把数据源从直连 WS 改为 store。

### 5.4 live2d_stream（轻接入）

- 只 `subscribe("live2d:", fn)`、`subscribe("audio:", fn)`。
- 口型、表情、动作逻辑不变，数据源统一到 store。

## 6. 错误处理

- WS 断线：立即一次 HTTP 拉取 → 指数退避重连 → 重连成功拉快照（seq 合并防回退）。
- 命令失败：`command_failed` 显式显示 error，不静默。
- HTTP 拉取失败：显示"离线"徽章，不覆盖已有 store 状态。
- 快照过期：按 `lastSnapshotSeq` 丢弃，不覆盖更新状态。

## 7. 测试

- 后端单测（pytest）：
  - EventBus seq 单调性（含并发发布）。
  - `state:changed` 快照发布与五类触发条件。
  - `command_id` 生成与透传（Command → 调度官 → 事件）。
  - 快照 `version` = 生成时 seq。
  - `/api/state`、`/api/metrics` 携带 version。
- 前端 store 纯逻辑（node 可跑，可选）：seq 合并、双游标、命令状态机。
- 回归：现有 253 项 pytest 全通过；四页面手动验收。

## 8. 验收清单

1. 前端只发事件，不预测数值：切换开关后 UI 不立即变化，`state:changed` 到达后才更新。
2. 后到覆盖先到：乱序事件按 seq 合并，旧事件不覆盖新状态。
3. 快照可回填：加载中收到 WS 事件，快照仍能正确初始化（双游标）。
4. 助手 prompt 实时性：切换角色后立即对话，角色为最新值。
5. 四页面功能不退化：开关、命令、角色切换、成本、看门狗、事件日志均正常。
6. 断线恢复：WS 断开重连后状态一致，无回退。

## 9. 不做（YAGNI）

- 不做多标签页同步（未来 BroadcastChannel）。
- 不做增量事件流（方案 B）。
- 不做前端构建链 / 框架。
- 不做快照按页面订阅分发（本期全量，预留过滤参数）。
