# Future Scene V2 控制界面与智能助手合并设计文档

日期：2026-08-14
状态：已批准（用户确认全部决策点后进入实施）

## 1. 背景与目标

系统有两个前端页面：`/dashboard/`（控制界面：概览/调度官/事件流/系统状态）与 `/assistant/`（智能助手：对话工作台/调度官调试/日程/系统总览/配置管理）。两页均基于方案 A 重构后的同一套状态层（`store.js`/`state_sync.js`/`tokens.css`），但各自创建独立的 store 实例与 WS 连接。

目标：合并为单页面单入口（`/dashboard/`），实现一个页面、一个 store、一条 WS 连接；旧 `/assistant/` 重定向到 `/dashboard/#assistant`。

## 2. 关键决策（用户已确认）

| 决策点 | 结论 |
|---|---|
| 合并形态 | 助手成总控台新视图（第五个视图） |
| 功能范围 | 助手全部功能迁入，无功能退化 |
| 旧地址处理 | `/assistant/` 重定向到 `/dashboard/#assistant` |
| 实现路线 | 路线 A：内嵌迁移（单一 store / 单 WS） |
| 导航组织 | 去重合并（6 项导航） |

## 3. 合并后导航结构（6 项）

| 侧边栏项 | 来源 | 内容 |
|---|---|---|
| 智能助手 | assistant `view-chat` | 对话工作台（聊天 + 状态面板 + 语音输入） |
| 系统总览 | dashboard `master` + assistant `overview` 合并 | KPI（成本/调用/在线/状态）+ 会话/降级快照 + dashboard `state` 并入 |
| 调度官管理 | dashboard `orchestrators` + assistant `modules` 合并 | 开关列表 + 健康徽章（数据同源，保留 dashboard 版式） |
| 事件流 | dashboard `events` | 按 seq 排序的事件日志 |
| 日程设置 | assistant `schedule` | 日程管理（迁入） |
| 配置管理 | assistant `config` | 配置面板（迁入） |

dashboard 原有 `state` 视图并入"系统总览"（会话/开关/降级快照与 master 重叠，避免重复）。

## 4. 技术要点

### 4.1 单一 store
- dashboard 保留现有 `store` 实例，assistant 的 `store` 删除。
- assistant 全部 `store.subscribe` / `getState` / `dispatch` 逻辑改用 dashboard 的 store。
- 合并后所有视图共享同一状态与同一 WS 连接。

### 4.2 视图迁移方式
- dashboard 侧边栏 nav 增加：智能助手、日程设置、配置管理。
- assistant 的 `view-chat`、`view-schedule`、`view-config` HTML 结构迁入 dashboard 对应 `.view`。
- assistant 独有 CSS 类（`.chat-wrap`、`.chat-main`、`.mic-btn` 等）并入 dashboard `<style>`。
- assistant 独有 JS（对话渲染、语音识别、日程操作、配置读写）迁入 dashboard `<script>`；命名冲突（`$`/`esc`/`toast` 等）去重——保留 dashboard 版本，删除 assistant 副本。

### 4.3 顶栏与连接状态
- dashboard 顶栏沿用，增加角色/场景显示（`topbarRole`/`topbarScene`）。
- `state_sync.js` 的 `onStatusChange` 回调供所有视图共用连接状态展示。

### 4.4 路由与重定向
- `app_factory.py` 中 `/assistant/` 改为 `redirect("/dashboard/#assistant")`。
- dashboard 支持 hash 定位：加载时读 `location.hash`（如 `#assistant`）激活对应视图。
- `frontend/assistant/index.html` 删除。

## 5. 功能不退化清单

- 对话：消息流式渲染、command_id 状态机、导出/清空对话、建议 chip、Enter/Ctrl+Enter 发送。
- 语音输入：Web Speech API 保留。
- 对话历史：localStorage 单标签（合并后仍单标签，键不变）。
- 状态面板：角色/开关/成本/看门狗实时（走 store）。
- 日程设置、配置管理：完整迁入。

## 6. 测试与验收

- 后端：pytest 全量（`/assistant/` 重定向断言更新）。
- 前端：node 语法检查合并后的 dashboard 脚本。
- 手动验收：`/dashboard/` 六视图切换、对话链路、语音、日程、角色切换实时性、`/assistant/` 重定向。

## 7. 不做（YAGNI）

- 不做路线 C 的共享模块抽取（后续若代码量再膨胀再拆）。
- 不做多标签页支持（维持单标签）。
- 不合并 `subtitle_overlay`/`live2d_stream`（直播展示页，独立窗口合理）。

## 8. 验收清单

1. `/dashboard/` 六视图（智能助手/系统总览/调度官管理/事件流/日程设置/配置管理）切换正常。
2. 对话链路完整：发送 → command_id 状态机 → 流式回复渲染。
3. 语音输入可用。
4. 角色切换 → `state:changed` → 前端实时更新。
5. `/assistant/` 重定向到 `/dashboard/#assistant` 且自动激活智能助手视图。
6. 单一 WS 连接（无重复连接）。
7. 全部 pytest 通过。
