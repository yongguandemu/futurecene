# 直播间集成主控制界面设计文档

日期：2026-08-14
状态：已批准（用户确认：iframe 嵌入视图 / 新增导航项 / 智能助手联动直播准备）

## 1. 背景与目标

直播测试台（live2d_stream 升级版）已完成并验证（弹幕注入→LLM→字幕→TTS→Live2D 口型全链路）。用户希望将其集成进主控制界面（dashboard），并实现"智能助手帮做直播界面准备"的联动能力。

目标：
1. dashboard 侧边栏新增"直播间"导航项，主内容区 iframe 嵌入 `/live2d/`。
2. 通过智能助手对话可驱动直播准备：切换角色模型、表情、动作、"准备直播界面"。
3. 保留 `/live2d/` 独立页面（OBS 浏览器源用）。

## 2. 关键决策（用户已确认）

| 决策点 | 结论 |
|---|---|
| 集成形态 | iframe 嵌入视图 |
| 旧入口处理 | dashboard 无旧直播入口，新增导航项 |
| 智能助手联动 | 对话驱动直播准备（角色/模型/表情/动作/准备指令） |
| 独立页面 | `/live2d/` 保留供 OBS 使用 |

## 3. 设计

### 3.1 dashboard 集成

- 侧边栏新增 nav-item（`data-page="live"`），放"智能助手"之后。
- 新增 `<section class="view" id="view-live">`：`<iframe src="/live2d/" width="100%" height="100%" style="border:none">`。
- `PAGE_TITLES` 增加 `live: "直播间"`。
- hash 定位机制（`initHashRoute`）自动支持 `#live`。

### 3.2 智能助手联动（直播准备意图）

**intent_parser.py 新增 4 条意图规则**：

| 意图 | 正则 | capability | payload |
|---|---|---|---|
| 加载模型 | `^加载?(?:模型)?\s*(hiyori|小恶魔|lilith|yuki)$` | `live2d:load` | `{model_name}` |
| 表情 | `^做(?:个)?(?:开心|难过|惊讶|害羞|生气|平静)(?:的表情)?$` | `live2d:expression` | `{expression}` |
| 动作 | `^(挥挥手|挥手|点头|摇头|打招呼)$` | `live2d:motion` | `{motion}` |
| 准备直播 | `^准备(?:一下)?直播(?:界面)?$` | `live2d:prepare` | `{}` |

**live2d:prepare 处理**：CommandRouter 不特殊处理，直接路由到 live2d orchestrator？不——prepare 是编排动作（load + idle + 字幕确认）。选择：在 CommandRouter 中识别 `live2d:prepare`，拆分为：live2d:load（当前角色）→ 发布字幕"直播界面已就绪"。但 CommandRouter 不应做编排。更简单方案：`live2d:prepare` 直接作为 capability 交给 live2d orchestrator？orchestrator 无字幕能力。

决定：prepare 拆解放在 CommandRouter（指挥官层编排）：dispatch 时若 capability == "live2d:prepare"，先调 live2d:load（当前 session.role），再发布 FRONTEND_SUBTITLE_UPDATE（"直播界面已就绪，可以开播啦～"），返回 ok。这符合指挥官职责（编排领域能力）。

**model_name 角色映射**（前端 ROLES 决定实际加载；后端 live2d:load 仅记录状态）：
- yuki → Hiyori
- lilith → 小恶魔

### 3.3 live2d_stream 前端联动

iframe 内页面自带 WS 连接（state_sync），无需 postMessage：
- 订阅 `session:switched`：按新角色加载对应模型（复用现有 ROLES 配置，先隐藏旧模型再加载新模型）。
- 订阅 `live2d:loaded`：后端装载通知（当前仅状态记录，前端可忽略或显示 hint）。
- 订阅 `live2d:expression_changed` / `live2d:motion_triggered`：已有（handleEvent）。

角色切换模型加载：新增 `switchActor(role)`：若该角色 actors 已存在直接 setState；否则按 ROLES 配置加载模型并加入 actors。

### 3.4 数据流

```
对话"切换到 Lilith" → /api/command → session:switch → session:switched ──WS──▶ iframe: switchActor('lilith')
对话"做个开心的表情" → /api/command → live2d:expression → live2d:expression_changed ──WS──▶ iframe: 表情映射
对话"准备直播界面" → /api/command → (router 编排) live2d:load + FRONTEND_SUBTITLE_UPDATE ──WS──▶ iframe 就绪 + 字幕
```

## 4. 测试与验收

- pytest：intent_parser 新增意图单测；live2d:prepare 路由编排单测；全量回归。
- 前端：node 语法检查；手动验收 5 项（侧边栏入口、角色切换、表情、准备直播、/live2d/ 独立访问）。

## 5. 不做（YAGNI）

- 不做 OBS/VTS 桥接、"一键开播"流媒体编排。
- 不做流式字幕改造。
- dashboard 与 iframe 各自独立 WS 连接（直播间作为独立单元可被 OBS 单独引用）。

## 6. 验收清单

1. dashboard 侧边栏出现"直播间"项，点击嵌入 live2d 页面正常。
2. 对话"切换到 Lilith"→ iframe 内模型切换。
3. 对话"做个开心的表情"→ iframe 表情变化。
4. 对话"准备直播界面"→ 模型就绪 + 字幕确认。
5. `/live2d/` 独立访问仍正常。
6. 全量 pytest 通过。
