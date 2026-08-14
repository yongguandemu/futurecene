# AGENTS.md — Future Scene V2 开发规范

本文件是 AI 助手与本项目协作者在**每次写代码前**必须阅读的规范指令。完整背景见《架构实施规格书.md》（本文件是其可执行摘要，冲突时以规格书为准）。

## 项目定位

AI 虚拟主播直播系统：用户通过自然语言对话控制虚拟角色（yuki / lilith）完成直播操作（回复弹幕、切换场景、点歌、开播/下播、游戏实况等）。架构为**指挥官（Commander）- 调度官（Orchestrator）**两级大脑。

## 目录结构

```
src/
├── commander/      指挥官层：intent_parser / command_router / session_context /
│                   switch_manager / orchestrator_registry / danmaku_pipeline /
│                   state_publisher / cost_tracker / cost_circuit_breaker /
│                   degradation_manager / character_profile / protocols
├── orchestrators/  调度官层：每个领域一个目录（目录即容器）
│   ├── llm / tts / live2d / bilibili / memory / safety / screen_control / game
│   ├── music / platform / stream / experience / live_intelligence   (P2 扩展)
│   └── collaboration/  多角色协作域：arbitrator / rules / turn_tracker /
│                       triggers / context_manager / coordinator
├── shared/         共享层：event_bus / events / decision_policy / decision_log /
│                   config_loader / logger / watchdog / data_store / crash_reporter
└── web/            API 网关：app_factory + routes/{command,health,state,switch,ws}
frontend/           纯原生前端：dashboard / subtitle_overlay / live2d_stream / assets
tests/              pytest 测试（与 src 目录结构对应）
架构实施规格书.md    唯一权威规格
AGENTS.md           本文件
```

## 六大设计纪律（违反即破坏架构）

| # | 纪律 | 要求 |
|---|------|------|
| D1 | 指挥官唯一 | 系统只有一个指挥官（src/commander），负责分发与状态管理 |
| D2 | 调度官独立 | 调度官之间**禁止直接 import 对方模块**；跨域协作走事件或指挥官编排 |
| D3 | 模块傻 | 模块被动工作：不轮询、不自启动、不维护跨领域状态；被调用即工作、完成后立即返回 |
| D4 | 开关集中 | 功能开关统一在 switch_manager，**调用调度官前必须先查开关** |
| D5 | 事件驱动通知 | 模块间通知走 EventBus，事件名必须在 `src/shared/events.py` 注册并收录 `ALL_EVENTS` |
| D6 | 注册表驱动扩展 | 新增功能 = 新建目录 + 注册一次，不修改已有模块 |

## 模块 8 项契约（每个 Python 模块必须满足）

每个模块（含 `__init__.py`）的文件头部 docstring 必须包含完整 8 项，缺一项即不合规。模板：

```python
"""xxx.py — 模块名（所属域）

一句话职责。

# 模块内容清单 — <模块名>

## 1. 模块身份标识
- 所属调度官：<orchestrator 名>
- 能力名：<capability 名>

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |

## 3. 输入契约
- 输入格式：<方法签名>
- <参数说明>

## 4. 输出契约
- 成功：<返回值>
- 失败：<失败返回值>
- 事件：<发布/订阅的事件>

## 5. 依赖声明
- 外部服务：<无 / 具体服务>
- 内部模块：<依赖的模块>
- 预先配置：<前置条件>

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |

## 8. 领域状态说明
- 状态项：<内部状态>
- 持久化：<无 / 文件等>
- 恢复：<重启后如何恢复>
"""
```

新增模块时把 8 项作为编写指令的一部分；改动模块时同步更新对应契约。

## 通信规范

- **命令调用**（同步）：指挥官 → 调度官，经 `handle({"capability", "payload"}) -> {"ok", "data", "error"}`（OrchestratorProtocol）。调度官之间**禁止**命令调用。
- **事件通知**（异步）：EventBus 发布/订阅。命名 `{domain}:{action}` 全小写。**新增事件必须**：在 `events.py` 定义常量 + 收录进 `ALL_EVENTS`（否则 EventBus 校验拒绝），禁止在业务代码手写事件字符串。
- 大对象（音频/图片）不进事件：写 `data/cache/`，事件只传 ID/路径。

## 决策分级与上抛（新增能力必须登记）

决策归属四级模型（实现于 `src/shared/decision_policy.py`）：

- L0 反射：硬规则拦截/放行（脏话过滤 `safety:check_input`），零咨询
- L1 域内自治：只影响本域、可逆、低成本，调度官自己拍板（插火把 `experience:*`、切歌 `music:*`、截图 `screen:*`）
- L2 仲裁上抛：跨域冲突/资源互斥（发言权 `collab:*`），确定性规则链
- L3 总脑编排：需全局上下文或高风险不可逆（回复弹幕 `llm:chat`、开播/下播 `stream:start/stop`、切换角色 `session:switch`）

新增调度官能力时，**必须**在 `DECISION_MATRIX` 登记归属层（支持 `*` 通配前缀）；未登记走三问推断。决策点（仲裁/静默/拦截/上抛）**必须**写入 `src/shared/decision_log.py`（事件 `decision:logged`），`no_action` 必须带 reason_code 说明「为何不回应」，区分「系统没收到」与「系统决定不回应」。

## 测试规范

- 所有测试在 `tests/`，pytest，命名 `test_<模块>.py`。
- **新增/修改功能必须配套测试**，提交前跑全量：`python -m pytest tests -q`（当前 343 个测试必须全绿）。
- 事件 schema 有独立测试（`test_events_schema.py`）校验唯一性/命名/ALL_EVENTS 一致性，改 events.py 必须同步。
- 单测风格：顶部 `sys.path.insert` 项目根，纯函数断言，EventBus 单例用 `reset()` 隔离。

## 前端规范

- 纯原生 HTML/CSS/JavaScript，**禁止框架**；设计令牌统一用 `frontend/assets/tokens.css`（dark/light 双主题）。
- 双角色（yuki/lilith）是系统基线：渲染页按 `role` 路由口型/动作，状态经 `frontend/assets/store.js` 的 `characters` 段同步（`speech:arbitrated`/`speech:completed` 更新 speaking）。
- 前端页面与后端交互仅走：`POST /api/command`、`WS /ws/events`、`GET /api/state`、`GET /api/metrics`、`GET /api/decisions`。

## 常用命令

```powershell
python -m pytest tests -q          # 全量测试（提交前必须跑）
python -m pytest tests/test_xxx.py # 单文件
python src/app.py                  # 启动系统（需先配置 .env）
```

## 提交规范

Conventional Commits 前缀：`feat` / `fix` / `docs` / `test` / `refactor` / `ops`，中文描述，如 `feat(ops): 决策日志与策略模块（decision_log/decision_policy + 接线 + 文档）`。
