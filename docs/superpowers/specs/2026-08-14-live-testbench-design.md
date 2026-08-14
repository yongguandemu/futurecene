# 聊天直播测试台设计文档（升级 live2d_stream + 启用主动对话）

日期：2026-08-14
状态：已批准（用户确认：升级 live2d_stream / 启用主动对话 / OBS 拼接为兜底）

## 1. 背景与目标

用户需要"聊天直播"在新系统中可用：LLM 回复（主动+被动）、TTS 朗读（出声）、Live2D 驱动、消息输入框（测试）、弹幕显示框（测试）、直播间背景。

现状探索结论（Explore agent 报告）：
- LLM `llm:chat` 可用（双入口）；`llm:active_dialogue` 实现完整但生产未启动（死代码）
- TTS 合成可用（已产出 wav），但**无前端播放器**——`/ws/tts_audio` 无客户端连接（断点 1）
- Live2D 前端链路完整（PixiJS + WS + 口型计时），但后端 `live2d:load` 生产零调用，后端模型状态恒空（断点 2）
- 弹幕测试注入仅有 `demo_danmaku.py`；dashboard 输入走 command 源不触发字幕/TTS/口型（断点 3）
- `assets/streaming/` 素材存在但前端零引用

目标：将 `live2d_stream` 升级为完整直播测试台，打通 6 项能力，端到端可演示。

## 2. 关键决策（用户已确认）

| 决策点 | 结论 |
|---|---|
| 测试台形态 | 升级 `live2d_stream` 页面 |
| 主动对话 | 启用 ActiveDialogue（冷场救星） |
| 兜底方案 | 若 Web 集成受阻，用户可在 OBS 中用 `assets/streaming/` 素材手工拼接 |

## 3. 设计

### 3.1 后端接线

**3.1.1 新增测试弹幕注入端点 `POST /api/danmaku`**
- 文件：`src/web/routes/danmaku.py`（新）或并入 `command.py`
- 输入：JSON `{content, user_name, user_id}`；发布 `danmaku:received`（复用 normalizer 同构 payload：`event_type="danmaku", content, user_name, user_id, extra={}, timestamp`）
- 输出：`{ok: true, command_id: 新生成}`（供前端追踪）
- 注册到 `app_factory.py`

**3.1.2 启用主动对话（ActiveDialogue）**
- 文件：`src/app.py`
- 装配时（不再依赖 COLLAB_ENABLED）：
  - `llm_orch._active` 已存在（构造时创建，`set_generator` 通用生成器已注入）
  - 调用 `active_dialogue.set_event_bus(event_bus)` + `active_dialogue.start()`
  - 订阅 `dialogue:active` 事件 → 触发"主动发言"链路（字幕 + TTS + 口型）
- 主动发言处理：新增 `src/commander/active_speaker.py`（订阅 ACTIVE_DIALOGUE → 发布 FRONTEND_SUBTITLE_UPDATE + 调用 tts handle）——或复用 DanmakuPipeline 增加 `_speak_active(text)` 方法。采用后者（复用 tts/safety 注入，最小改动）。

**3.1.3 装配 `live2d:load`**
- 文件：`src/app.py`
- 启动时对当前角色（session.role 默认 yuki）调用 `registry.get("live2d").handle({capability: "live2d:load", payload: {role}})`，使后端模型状态非空，`lip_sync_start/end` 事件真正发出

**3.1.4 TTS 播放修复（前端为主，后端不变）**
- `/ws/tts_audio` 端点已存在（按 audio_id 回传字节），前端补客户端即可

### 3.2 前端 `live2d_stream/index.html` 升级

**3.2.1 直播间背景装饰层**
- 使用 `assets/streaming/` 素材：`banner_top.png`（顶部）、`bar_bottom.png`（底栏）、`btn_coin/follow/like.png`（按钮）、`panel_danmu.png`（弹幕面板背景）
- 页面布局：透明背景保持（OBS 可用），Web 测试台叠加装饰层
- 静态资源已由 `/assets/<path>` 路由提供

**3.2.2 弹幕输入框（测试用）**
- 页面底部输入框 + 发送按钮 → `POST /api/danmaku`（携带 content/user_name 默认"测试观众"）
- 保留现有 `hint` 提示

**3.2.3 弹幕显示框**
- 订阅 `danmaku:received`（显示观众弹幕）、`audience:filtered`（显示被拦截）、`frontend:subtitle_update`（显示 AI 字幕）
- 最近 N 条（如 50）循环展示，新弹幕插入顶部

**3.2.4 TTS 播放器**
- 订阅 `tts:audio_ready` → 已有 `handleLipSync`（口型计时），新增：连接 `/ws/tts_audio` 发送 `{audio_id}` → 收字节 → Blob → `new Audio(URL.createObjectURL(...))` 播放
- 每角色一个 `Audio` 实例，`audio_id` 去重

**3.2.5 Live2D 渲染**
- 现有链路保留（ROLES 硬编码、boot 加载 model3.json、WS 订阅、expressionMap/motionMap）

### 3.3 数据流（端到端目标态）

```
测试弹幕输入 → POST /api/danmaku → danmaku:received
  → DanmakuPipeline（safety→memory→llm:chat→safety）
      → FRONTEND_SUBTITLE_UPDATE ──▶ 前端弹幕/字幕显示
      → tts:synthesize → tts:audio_ready
          ├─▶ 前端 handleLipSync（口型计时）+ /ws/tts_audio 播放（出声）
          └─▶ 后端 Live2DOrchestrator（模型已 load）→ live2d:lip_sync_start/end
冷场触发 → ActiveDialogue.tick → dialogue:active → 主动字幕+TTS+口型（同链路）
```

## 4. 测试与验收

- 后端：`POST /api/danmaku` 发布事件断言（pytest 新测试）；全量回归
- 前端：node 语法检查；端到端手动验收（输入弹幕 → AI 字幕 + 有声 TTS + 口型；冷场触发主动说话）
- Live2D 原理说明交付：加载/驱动/口型/表情映射的文档（随交付总结给出）

## 5. 不做（YAGNI）

- 不做真实 B站连接（`bilibili:connect` 保持未启用，测试台用模拟弹幕）
- 不做 `llm:stream_chat` 流式字幕（当前非流式 `llm:chat` 已够用，延迟 ~5s）
- 不接 `live_intelligence` 反应器（"第二个大脑"保持孤岛，后续单独任务）
- 不做 OBS/VTube Studio 桥接（用户兜底方案自行处理）

## 6. 验收清单

1. `POST /api/danmaku` 返回 ok + command_id；发布 `danmaku:received`
2. 弹幕输入 → AI 回复字幕显示
3. TTS 出声（Audio 播放，非静默）
4. Live2D 口型随 TTS 动（后端 lip_sync_start 事件真实发出）
5. 冷场超过阈值 → 主动说话（字幕+TTS+口型）
6. 直播间背景装饰层显示
7. 全部 pytest 通过
