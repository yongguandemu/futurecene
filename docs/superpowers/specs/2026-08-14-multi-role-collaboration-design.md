# 多角色联合方案设计（Multi-Role Collaboration）

> 日期：2026-08-14 · 项目：Future Scene V2 · 状态：待评审
> 关联：`docs/superpowers/plans/2026-08-14-frontend-rework.md`（前序重构，本方案在其上叠加）

## 目标

让 Yuki 与 Lilith 两个角色**同时在场、同屏出现、独立接收指令、轮流或协同发言**，并支持**完整双人对话**（互相接话形成多轮）。

## 约束（用户确认）

1. 不推翻现有指挥官-调度官 + EventBus 架构，单角色模式零回归。
2. 所有"多角色如何协同"的逻辑**集中在一个模块组** `src/orchestrators/collaboration/`（与调度官平级），不分散到调度官/指挥官角落。
3. 仲裁器**零额外 LLM 调用**：规则全部为确定性逻辑（关键词/意图/冷却/随机），可测可控。
4. 仲裁规则**可插拔**（新增角色/群聊/角色-观众联动可复用）。
5. 联动逻辑**可独立测试**，不依赖真实 LLM/TTS。
6. 冷场自发闲聊：`active_dialogue.py` 机制评估为成熟（定时触发/冷却/静默检测/生成器注入/话题池兜底齐全），纳入设计但作为 **P3 增强阶段**，依赖核心仲裁稳定后接入。

---

## 1. 现状分析与既有缺口（代码扫描结论）

| 模块 | 现状 | 缺口 |
|---|---|---|
| `session_context.py` | 单 `role` 字段，`VALID_ROLES={yuki,lilith}`，`switch_role` 发布 `session:switched` | 无"在场角色集合"概念 |
| `character_registry.py` | **V2 不存在**（仅旧项目 `LumiProject/core/` 有注册/查询/切换实现） | 需按 V2 架构重建（数据源 `config/profiles/`） |
| `config/profiles/{yuki,lilith,lumi}/` | character.yaml + system_prompt.txt + tts_config.yaml + catchphrases.json 齐全 | **无运行时加载器**，无人读取 |
| `danmaku_pipeline.py` | 调 `llm:chat` 时**未注入 system_prompt**（LLM 注释写明"人格由指挥官注入"但无人注入） | 人格注入层缺失，角色无差异 |
| `tts_orchestrator.py` | `_voice_map` 已含 yuki/lilith 音色，按 payload.role 选音色 | `tts:audio_ready` 事件**不带 role**，口型无法路由 |
| `live2d_orchestrator.py` | 单模型状态机（`_model` 一个），`_on_audio_ready` 无条件触发口型 | 多模型实例化 + 事件带 role |
| 前端 `live2d_stream/` | `MODEL_URL` 硬编码"小恶魔"，单 PIXI stage 单模型居中 | 双模型同台渲染 |
| 前端表情/动作 | `setExpression` 直接 `model.expression(语义名)`，**无映射表**（小恶魔中文表情名"头发/唱歌…"，语义"开心"等静默失败） | 需 per-model `expression_map`/`motion_map`（现有潜在缺陷 + 跨模型适配点） |
| 前端 store/assistant | `session.role` 单值；assistant 已有角色卡片 UI | 状态需按角色维度扩展 |
| 资产 | V2 `assets/live2d/` 仅有"小恶魔"模型 | 双角色形象：Yuki 迁移旧项目 Hiyori；Lilith 沿用现有"小恶魔"；Haru 留作备选 |

---

## 2. 角色在场模型（核心概念转变）

从"单角色切换"升级为"角色集合在场"：

```python
# SessionContext 扩展（向后兼容：单角色模式 = present_roles 大小为 1）
present_roles: Set[str]      # 在场角色集合，如 {"yuki", "lilith"}
lead_role: str = "yuki"      # 主角色：仅系统意图（下播/状态等）归属，不独占发言权
role: str                    # 保留：焦点角色（兼容现有逻辑/前端展示）
characters: Dict[str, CharacterState]  # 各角色在场/发言/模型状态
```

规则：
- `switch_role(role)` 保留语义 = 设置焦点角色（前端高亮/字幕默认归属）；`add_role/remove_role` 管理在场集合。
- **"切换到 Lilith"指令的语义（多角色模式）**：`session:switch` 仅设置焦点角色 + 可同步设 `lead_role`，**不改变在场集合**（两角色仍同时在屏）。回到单角色独占需显式指令（如 `!独占Lilith`）或 UI 取消另一角色在场（`remove_role`）——由 `collaboration.enabled` 决定：enabled=false 时 `switch_role` 行为与现状完全一致（单角色切换）。
- 单角色模式（`present_roles` 大小为 1）：`danmaku_pipeline` 走原逻辑（`_current_role()`），零改动。
- 多角色模式：发言角色由仲裁器指定，`execute_with(role)` 显式传入。

---

## 3. 架构总览与数据流（方案 A：协作层前置 + 链路参数化复用）

```
弹幕/指令 ──▶ collaboration/coordinator（跨领域联动，与调度官平级）
                 │ 仲裁链：@指定 → 意图 → 相关性 → 冷却 → 随机（零 LLM）
                 ▼
              arbitrator 产出"谁回应"（发布 speech:arbitrated）
                 │
                 ▼
   DanmakuPipeline.execute_with(text, role=X, system_prompt=X人格, turn_context)
   （safety → memory[X分桶] → LLM[X人格] → subtitle[带X] → TTS[X音色]）
                 │
                 ▼
         tts:audio_ready(role=X) ──▶ live2d 模型X口型 / 字幕带X / 前端按X渲染
                 │
                 ▼
        triggers 监听发言完成（speech:completed）→ 接话/吐槽/引用/补充决策
                 │  → 回仲裁器申请 Y 发言（带"引用 X 刚才的话"上下文）
                 ▼
         turn_tracker（话轮/冷却/互斥）＋ context_manager（全局流，供感知彼此）
```

设计要点：
1. **联动集中**：`collaboration/` 是唯一的多角色协同逻辑宿主；调度官与指挥官只做参数/事件透传，不加协同逻辑。
2. **链路复用**：safety/memory/LLM/subtitle/TTS 编排全部复用 `DanmakuPipeline`，仅参数化注入 `role/system_prompt/turn_context`。
3. **角色归属贯穿事件链**：`tts:audio_ready`、`live2d:*`、字幕事件全部携带 `role`，前端才能路由口型/字幕/音色。

---

## 4. 联动模块组（新增 `src/orchestrators/collaboration/`）

```
src/orchestrators/collaboration/
├── __init__.py          # 对外导出 CollaborationCoordinator
├── arbitrator.py        # 发言权仲裁器（核心）
├── context_manager.py   # 多角色上下文管理（独立记忆 + 全局对话流）
├── turn_tracker.py      # 话轮追踪（谁在说/谁在等/冷却）
├── rules.py             # 仲裁规则集（可插拔 Rule 基类 + 具体规则）
├── triggers.py          # 联动触发条件（接话/吐槽/引用/补充）
└── coordinator.py       # 顶层协调器（组装全部组件，对外统一接口）
```

### 4.1 `arbitrator.py` — 发言权仲裁器（核心）

```python
class SpeakerArbitrator:
    def __init__(self, rules, turn_tracker): ...
    def arbitrate(self, request: SpeechRequest) -> Optional[str]:
        """依次应用规则链，返回获权角色或 None（无人回应）。"""
    def is_mutex_held(self, role: str) -> bool: ...   # 同一时刻仅一人发声
```

- 输入 `SpeechRequest`：`{source: danmaku|collab|active, text, user_name, requester_role, kind}`。
- 输出：放行角色；若当前有人正在发言（互斥），请求进入 `turn_tracker` 待发队列，完成后按队列仲裁。
- **互斥保证**：`turn_tracker` 记录 `current_speaker`，仲裁放行前校验；发言完成（`speech:completed`）释放。
- 每次仲裁发布 `speech:arbitrated(role, rule_hit, request_id)` 供前端展示与调试。

### 4.2 `rules.py` — 可插拔规则集

```python
class Rule:                      # 基类
    name: str
    def evaluate(self, ctx: ArbitrationContext) -> RuleVerdict
    # RuleVerdict: {role | None, confidence, reason}
```

内置规则（默认优先级链，配置可调顺序）：

| 优先级 | 规则 | 逻辑 | 示例 |
|---|---|---|---|
| 1 | `MentionRule` 手动指定 | 弹幕含 `@yuki`/`@lilith` 或"Yuki你看"/"Lilith你怎么看" → 放行指定角色（硬放行） | "@Lilith 你同意吗" → Lilith |
| 2 | `IntentRule` 意图类型 | 系统意图（下播/状态/点歌/感谢）→ `lead_role` | "下播" → Yuki（lead） |
| 3 | `RelevanceRule` 相关性 | 弹幕分词与各角色 profile 关键词（personality/catchphrases/话题词）加权打分，最高者 | "Yuki讲个笑话" → Yuki |
| 4 | `CooldownRule` 冷却 | 谁闲置最久（`turn_tracker` 上次发言时间最早者）先说话 | Yuki 刚说完、Lilith 30s 未开口 → Lilith |
| 5 | `RandomRule` 随机扰动 | 前 4 条持平/无法决定时随机选择，**记录选择结果**，下次冷却优先取反 | 平局 → 随机，下轮偏向另一角色 |

- 相关性打分零成本实现：角色 profile 提供 `keywords` 字段（personality 标签 + catchphrases + 专属话题词），弹幕做简单分词/子串匹配加权。不做向量相似度（避免 embedding 调用，符合"零 LLM"约束；后续可扩展向量规则而不动框架）。
- 规则注册：`rules.register(Rule)` 按 name 覆盖，配置 `collaboration.rules_order` 控制优先级链。

### 4.3 `turn_tracker.py` — 话轮追踪

```python
class TurnTracker:
    current_speaker: Optional[str]      # 当前正在发声的角色
    pending_queue: List[SpeechRequest]  # 互斥期间的待仲裁请求
    last_speech_at: Dict[str, float]    # 各角色上次发言时间（冷却规则数据源）
    turn_history: List[TurnRecord]      # 话轮记录（谁、何时、类型、引用了谁）
    def acquire(role) -> bool           # 互斥获取
    def release(role) -> None           # 发言结束释放，触发待发队列仲裁
    def idle_seconds(role) -> float
```

### 4.4 `context_manager.py` — 多角色上下文管理

```python
class ContextManager:
    def memory_key(role) -> str             # 记忆分桶：memory:retrieve/store 按 character_id 隔离
    def global_transcript() -> List[Turn]   # 全局对话流（最近 N 条，跨角色）
    def build_system_prompt(role) -> str    # = profile.system_prompt + 感知彼此段（可选）
    def awareness_section(speaker, partner) -> str  # "你的搭档 Lilith 刚才说：…"（可配置开关）
```

- 记忆隔离：`MemoryOrchestrator` 现有 `memory:retrieve/store` 增加 `character_id` 参数，`context_manager` 按角色分桶（复用现有 memory 模块，不改其内部）。
- 感知彼此（可选，配置 `collaboration.awareness.enabled`）：注入对方在场信息 + 最近发言摘要，让回应角色"知道搭档刚说过什么"。

### 4.5 `triggers.py` — 联动触发条件

```python
class CollabTriggers:
    def evaluate(speech_completed: SpeechCompleted) -> List[TriggerProposal]
    # TriggerProposal: {role, kind: reply|banter|quote|supplement, reason, ref_text}
```

- 监听 `speech:completed(role, text)`，按配置概率与规则产出"让另一角色接话"的提案：
  - `quote` 引用：对方发言含可吐槽点（命中对方 catchphrase 关联词）
  - `banter` 吐槽：低概率插话（冷却达标时）
  - `supplement` 补充：主回应角色发言后，另一角色补充一句
- 提案回 `coordinator` → 构造 `SpeechRequest(kind=collab)` → 再进仲裁器（冷却/互斥约束下放行）。
- 防刷：`triggers` 全局冷却（如 20s 内至多 1 次接话，配置化）。

### 4.6 `coordinator.py` — 顶层协调器

```python
class CollaborationCoordinator:
    def __init__(self, event_bus, arbitrator, context_manager, turn_tracker,
                 triggers, pipeline, session, profiles): ...
    def start() / stop()                     # 订阅事件（danmaku:received、speech:completed、dialogue:active）
    def handle_danmaku(event, content, user_name)   # 弹幕入口：仲裁 → execute_with
    def handle_speech_completed(event, role, text)  # 触发接话决策
    def handle_active_dialogue(event, text, mood)   # 冷场闲聊入口（P3）
    def request_utterance(role, kind, reason, ref_text)  # 联动发言请求（triggers/外部调用）
    def snapshot() -> Dict[str, Any]         # 供 state_provider 聚合（characters/speaking）
```

- 仲裁产出角色 X 后：`pipeline.execute_with(text, role=X, system_prompt=ctx.build_system_prompt(X), turn_context=ctx.global_transcript())`。
- `handle_speech_completed`：由 `pipeline` 在链路末尾发布（或在 `llm:responded(role)` 上监听），`triggers.evaluate` → 提案 → `request_utterance`。

---

## 5. 新增模块清单

| 模块 | 职责 |
|---|---|
| `src/orchestrators/collaboration/`（6 文件） | 联动模块组（见第 4 节） |
| `src/commander/character_profile.py` | `config/profiles/{role}/` 加载器：system_prompt.txt / character.yaml（personality→关键词）/ catchphrases.json / tts_config.yaml；缓存 + 缺失兜底 |
| `src/web/routes/characters.py`（可选 P2） | `GET /api/characters` 返回在场角色元数据（模型 URL/颜色/音色），供前端双模型渲染与角色卡片 |

## 6. 修改现有模块清单

| 模块 | 修改内容 |
|---|---|
| `src/shared/events.py` | 新增 4 事件 + `ALL_EVENTS` 收录；`TTS_AUDIO_READY`/`LIVE2D_*`/`ACTIVE_DIALOGUE` 契约补 `role` 字段 |
| `src/commander/session_context.py` | `present_roles`/`lead_role`/`add_role`/`remove_role`/`set_lead`；`snapshot()` 扩展 `characters`；发布 `character:presence_changed` |
| `src/commander/danmaku_pipeline.py` | 新增 `execute_with(text, role, system_prompt, turn_context)` 参数化入口；`_chat` 注入 `system_prompt`；链路完成发布 `speech:completed(role)` |
| `src/commander/state_publisher.py` | 触发事件列表追加 `character:presence_changed`/`speech:arbitrated`/`speech:completed` |
| `src/web/state_provider.py` | 快照新增 `characters` 段（在场/说话/模型/表情），数据源 `coordinator.snapshot()` + `live2d.snapshot()` |
| `src/orchestrators/live2d_orchestrator/` | 单模型 → 多模型状态机 `_models: {role: ModelState}`；`live2d:load` 带 `role`；`_on_audio_ready` 按事件 `role` 路由口型；`LIVE2D_*` 事件带 `role`；`snapshot()` 按角色返回 |
| `src/orchestrators/tts_orchestrator/tts_orchestrator.py` | `TTS_AUDIO_READY` 发布补 `role`（合成时已知 payload.role） |
| `src/app.py` | 装配 `CharacterProfileLoader` + `CollaborationCoordinator` 并接线（多角色模式开关 `collaboration.enabled`） |
| `frontend/live2d_stream/index.html` | 双模型同台渲染（见第 9 节） |
| `frontend/assets/store.js` | `initialState` 增加 `characters`；快照合并同步扩展（双游标逻辑不变） |
| `frontend/assistant/index.html` | 角色卡片支持多角色在场；指令支持 `@角色` 定向；对话按角色着色 |
| `frontend/subtitle_overlay/index.html` | 字幕按 `role` 显示角色名（已有 role 元素，补样式区分双角色） |
| `config/config.yaml` | 新增 `collaboration` 域（enabled / rules_order / cooldown / trigger_probability / awareness）与 `roles[].live2d_model` 映射 |
| `assets/live2d/` | 新增 Lilith 模型（旧项目 Haru/Hiyori 迁移或新制），Yuki 沿用"小恶魔" |

---

## 7. 事件契约

新增事件（`events.py` + `ALL_EVENTS`）：

```python
CHARACTER_PRESENCE_CHANGED = "character:presence_changed"  # 角色在场变更（触发 state:changed）
SPEECH_ARBITRATED = "speech:arbitrated"                    # 仲裁结果：role/rule_hit/request_id
SPEECH_COMPLETED = "speech:completed"                      # 发言完成：role/text/audio_id（触发接话决策 + state:changed）
COLLAB_UTTERANCE_REQUESTED = "collab:utterance_requested"  # 联动发言请求：role/kind/reason/ref_text
```

字段透传（既有事件补 role）：

```python
TTS_AUDIO_READY         → + role
LIVE2D_LOADED/EXPRESSION_CHANGED/MOTION_TRIGGERED/LIP_SYNC_START/LIP_SYNC_END → + role
ACTIVE_DIALOGUE         → + role
FRONTEND_SUBTITLE_UPDATE → 已有 role（保持）
```

事件流时序（弹幕场景）：

```
danmaku:received
  → speech:arbitrated(role=X, rule_hit="mention|intent|relevance|cooldown|random")
  → llm:requested / llm:responded (role=X)
  → frontend:subtitle_update (role=X)
  → tts:audio_ready (role=X) → live2d:lip_sync_start (role=X)
  → speech:completed (role=X, text, audio_id)
    → [triggers] collab:utterance_requested (role=Y, kind=banter)
      → speech:arbitrated (role=Y) → …（同上，形成多轮）
```

---

## 8. 仲裁规则详细定义（零 LLM）

仲裁器收到发言请求后**依次应用规则链**，首个给出确定结论的规则获胜；全部无结论 → `RandomRule` 兜底。

1. **MentionRule（手动指定，硬放行）**：`@yuki`/`@lilith`、"Yuki/Yuki酱你怎么看"等显式指向 → 直接放行该角色（若该角色不在场则返回 None，不转派）。
2. **IntentRule（意图类型）**：系统/运营意图（下播、开播、状态、感谢、点歌、日程）→ `lead_role`。意图识别用现有 `intent_parser` 规则集（`_STATUS_RE`/`_POINT_SONG_RE` 等）+ 新增少量运营词表，不引入模型。
3. **RelevanceRule（相关性）**：弹幕文本对每个在场角色做关键词加权（`profile.keywords`），得分最高者胜出；得分差 < 阈值视为平局。

   **关键词格式定义（character.yaml v2.1 新增字段，评审确认）**：

   ```yaml
   keywords:                      # 新增（缺失时兜底推导，见下）
     personality: [温柔, 害羞, 认真]      # 人格标签（与 character.personality 对应）
     topics: [故事, 月亮, 邮差]           # 专属话题词（子串匹配）
     patterns:                            # 短语/正则扩展（子串或 regex: 前缀）
       - "讲个故事"
       - "regex:月亮.*邮差"
   ```

   - 匹配规则：弹幕依次命中 `patterns`（短语子串/正则）→ `topics`（子串）→ `personality`（子串），命中加权递减；`patterns` 命中权重最高。
   - **兜底推导**：`keywords` 缺失时从 `character.personality` 标签 + `character.catchphrase` + `speaking_style` 分词抽取生成（零配置可降级运行，规则质量受限于现有字段）。
   - `catchphrases.json` 是口癖语料（text/weight/position，用于 TTS/字幕口癖注入），**不承担相关性职责**。
   - 若后续需要更高精度，可在 `rules.py` 插槽加向量规则（不动框架），本期不做（避免 embedding 调用）。
4. **CooldownRule（冷却）**：`turn_tracker.idle_seconds(role)` 最大者先说话（"谁闲置久谁先说"）；`RandomRule` 上次选择记录在此取反。
5. **RandomRule（随机扰动）**：平局时随机；记录 `last_random_choice`，下次平局时偏向另一角色（anti-repeat）。

互斥与排队（评审确认的队列调度策略）：`current_speaker` 非空时新请求入 `pending_queue`；`speech:completed` 释放后仲裁。**同一时刻保证至多一个角色发声**（验收断言项）。

队列优先级（请求携带 `priority`，插队 + 同级 FIFO）：

| 优先级 | 请求来源 | 示例 |
|---|---|---|
| P0 | MentionRule（手动 @ 指定） | "@Lilith 你同意吗" |
| P1 | IntentRule（系统意图） | "下播" |
| P2 | RelevanceRule（相关性胜出） | "Yuki讲个故事" |
| P3 | Collab 接话/吐槽/补充 | 对方发言后的联动提案 |
| P4 | Active 冷场闲聊 | 无输入自发话题 |

规则：互斥释放后按 priority 升序取队首，同优先级按到达顺序（FIFO）。**v1 不支持硬打断**（当前发言播完才切换，切换点见第 9.1 节口型收尾）；P3 可选"软抢占"（当前音频播放 >70% 时，P0/P1 请求等待剩余 <1s 后切换）。

配置（`config.yaml` `collaboration` 域）：

```yaml
collaboration:
  enabled: false            # 默认关，开启即多角色模式
  rules_order: [mention, intent, relevance, cooldown, random]
  lead_role: yuki
  trigger_probability: 0.3  # 接话概率（初值建议，可调）
  trigger_global_cooldown: 20.0  # 接话全局冷却秒（初值建议，可调）
  awareness:
    enabled: true           # 感知彼此（system_prompt 注入对方最近发言）
    max_partner_lines: 2
```

**初值说明（评审确认）**：`trigger_probability: 0.3` + `trigger_global_cooldown: 20.0` 为建议起步值（约每 1-2 条发言有一次接话机会、20s 内至多 1 次联动），实施期需实测调优；若接话过频/过少，优先调这两项。

**运行中动态调整（评审确认）**：`ConfigLoader` 为启动时一次性加载（无热更新），故协调器持有 `_runtime_config`（初值 = config.yaml 的 collaboration 域），仲裁器/触发模块实时读取该对象；P2 提供 `POST /api/collab/config`（仅允许调整 `trigger_probability`/`trigger_global_cooldown`/`rules_order`/`awareness.*` 白名单项）覆盖运行时值，重启回落 config.yaml 初值。前端辅助页（P3）可暴露调节滑块。

---

## 9. 前端设计

### 9.1 `live2d_stream/index.html` — 双模型同台渲染

- 单 `PIXI.Application` stage，加载两个 `Live2DModel`（pixi-live2d-display 原生支持多模型）。
- 布局：左右分列（Yuki 左 / Lilith 右），位置/缩放由页面配置或 `/api/characters` 下发（`roles[].live2d_model`、`position`、`scale`）。
- 模型 URL：不再硬编码，从 `roles[].live2d_model` 映射（`/assets/live2d/{model}/{model}.model3.json`）。
- **模型来源（评审调整：Yuki=Hiyori，Lilith=小恶魔）**：
  - Yuki 形象：迁移旧项目 `Hiyori` 模型（核验：`Hiyori.model3.json` 为 Cubism 3 —— `Version:3` + moc3/physics3/pose3/cdi3 + 9 个 Idle 动作 + 标准 LipSync 参数组 `ParamMouthOpenY`，与 V2 运行时 pixi-live2d-display + live2dcubismcore 兼容）。迁移 = 拷贝 `assets/live2d/Hiyori/` 目录 + 配置映射，约 0.5 天。
  - Lilith 形象：**沿用 V2 现有"小恶魔"模型**（零迁移成本）；Haru 留作备选/第三形象。
  - **Hiyori 无表情文件**（model3.json 无 Expressions 段）→ `expression_map` 留空，语义表情事件映射缺失时跳过（不崩溃），表情表达靠动作 + 口型承担。
  - 若决赛时间紧，可全部共用单模型 + 换色/标签区分（零资产工期，仅做角色视觉标识）。
- **Hiyori 呼吸动作限制（评审确认：呼吸丰富需在驱动中限制）**：Hiyori 的 Idle 动作组（m01-m10）含较强呼吸循环，若交由运行时自动循环播放会过于抢眼。限制机制：
  - `roles[].restrict_breath: true`（Hiyori 默认开）+ `roles[].idle_silent_motion`（静默态专用低动作 idle，P1 实施时浏览器实测选定呼吸最弱的动作，如 `Hiyori_m03`/`Hiyori_m10`）。
  - 前端驱动改为**显式状态机**：`SILENT`（只播 `idle_silent_motion`，不依赖运行时自动 idle 循环）→ `TALKING`（talking 动作 + 口型）→ `SILENT`（200ms 过渡收尾）。未发言角色始终处于受控 SILENT，呼吸动作被压制。
  - 可选增强（P3）：参数层限制（锁定 `ParamBodyAngleX`/`ParamAngleX` 等呼吸参数摆动范围），实施时如 motion 控制已满足则不实现（YAGNI）。
- **per-model 表情/动作映射（评审确认，跨模型适配必做）**：不同模型的表情/动作命名各异（小恶魔表情为中文"头发/唱歌…"，动作 `wave/nod/shake/idle`；Hiyori 无表情、动作为 `Hiyori_m01..m10`），且现有前端 `model.expression(语义名)` 直接透传、无映射表（语义表情静默失败）。新增 `roles[].expression_map`/`motion_map`：

  ```yaml
  roles:
    - name: yuki
      live2d_model: Hiyori
      restrict_breath: true
      idle_silent_motion: Hiyori_m03      # 静默态低动作 idle（呼吸最弱，P1 实测选定）
      expression_map: {}                  # Hiyori 无表情文件 → 语义表情跳过
      motion_map: {wave: Hiyori_m01, nod: Hiyori_m02, shake: Hiyori_m05, idle: Hiyori_m01}
    - name: lilith
      live2d_model: 小恶魔
      restrict_breath: false
      expression_map: {开心: 唱歌, 难过: 流泪, 惊讶: 头发, 害羞: 嘟嘴, 生气: 脸黑, 平静: 头发}
      motion_map: {wave: wave, nod: nod, shake: shake, idle: idle}
  ```

  前端收到语义事件后先经映射表再调用 `model.expression/motion`；映射缺失时跳过（不崩溃）。此映射同时修复现有单模型的表情失效缺陷。
- 事件按 role 路由：`live2d:expression_changed(role)` → 对应模型表情；`live2d:motion_triggered(role)` → 对应模型动作；`live2d:lip_sync_start(role, audio_id)` → 对应模型口型。
- **口型切换平滑（评审确认：自然过渡 + 显式收尾）**：互斥保证不会两嘴同张；切换时前端在收到新角色 `lip_sync_start` 前，对旧角色模型执行收尾（口型归零 + 回 idle，约 200ms 过渡动画），再启动新角色口型。后端不硬重置（`LIP_SYNC_END` 已按时长自动触发），允许旧角色自然合嘴的轻微残留，保证视觉平滑。
- 静默态：未发言角色保持 idle 动画，可加"说话高亮"（边框/聚光）。

### 9.2 `frontend/assets/store.js`

- `initialState` 增加 `characters: {}`（`{yuki: {present, speaking, model, expression}, lilith: {...}}`）。
- 快照合并扩展 `next.characters = snap.characters`（双游标/乱序合并逻辑不变）。
- 事件订阅：`speech:arbitrated`/`speech:completed` 更新 `characters[role].speaking`。

### 9.3 `frontend/assistant/index.html`

- 角色卡片：在场角色可同时勾选（多角色在场配置），lead 标记。
- 指令定向：输入框支持 `@Yuki`/`@Lilith` 前缀 → 请求体带 `target_role`（走 MentionRule）。
- 会话区：消息按角色着色/头像区分；状态面板展示两角色发言状态（谁在说）。

### 9.4 `frontend/subtitle_overlay/index.html`

- 字幕带角色名（已有 `subtitle-role` 元素），双角色样式区分；新增发言角色标识。

---

## 10. 冷场自发闲聊（P3 增强，复用 active_dialogue）

- `active_dialogue` 机制评估：成熟（定时触发/冷却/静默检测/生成器注入/话题池兜底），**不改其触发逻辑**。
- 接入方式：`active_dialogue` 生成话题后发布 `dialogue:active`（补 `role` 字段）→ `coordinator.handle_active_dialogue` → 仲裁器决定谁先说（冷却/相关性）→ 按该角色人格生成（generator 角色化：prompt 带角色人设 + 对方最近发言）→ 发言完成后 `triggers` 允许对方接话 → 形成自发双人对话。
- 依赖：核心仲裁（P2）稳定后接入；`active_dialogue` 的 `set_generator` 增加角色参数支持（`generate(role)`）。
- 若后续主动对话机制重构，冷场互动随之迁移，不独立承担该逻辑。

---

## 11. 实施顺序（最小改动 → 完整实现）

### P0 · 基础层（数据/事件/配置）— 约 1-1.5 人日
1. `events.py`：4 新事件 + `TTS_AUDIO_READY`/`LIVE2D_*`/`ACTIVE_DIALOGUE` 补 role + `ALL_EVENTS` 收录。
2. `session_context.py`：`present_roles`/`lead_role`/`add_role`/`remove_role` + `snapshot()` 扩展。
3. `character_profile.py`：profiles 加载器（persona/keywords/voice/system_prompt，缓存 + 兜底）。
4. `state_provider.py` + `state_publisher.py` 触发列表扩展。
   - 回归：265 项测试全绿。

### P1 · 双模型渲染（前端可见性先行）— 约 2-2.5 人日
5. `live2d_orchestrator`：多模型状态机 + 事件带 role + 口型按 role 路由。
6. 资产与映射：迁移旧项目 `Hiyori` 至 `assets/live2d/Hiyori/`（约 0.5 天，含加载验证）；Lilith 沿用现有"小恶魔"；`config.yaml` 新增 `roles[]`（live2d_model/position/scale + `expression_map`/`motion_map` + `restrict_breath`/`idle_silent_motion`）。
7. `live2d_stream/index.html`：双模型同台渲染 + 表情/动作映射应用 + 呼吸限制状态机（SILENT/TALKING + 口型收尾 200ms）。
   - 验收点：双角色同屏、各自表情/动作/口型由带 role 的事件驱动（此时尚未发言，验证渲染与路由；同时修复现有单模型表情失效缺陷）；Yuki（Hiyori）静默时呼吸动作被压制为低动作 idle。

### P2 · 联动核心（完整双人对话）— 约 3.5-4.5 人日
8. `collaboration/` 六文件：rules → arbitrator → turn_tracker → context_manager → triggers → coordinator（TDD，每规则独立单测，含队列优先级单测）。
9. `danmaku_pipeline.py`：`execute_with` + system_prompt 注入 + `speech:completed` 发布。
10. `app.py` 装配 + `collaboration.enabled` 开关（单/多角色模式切换）+ `POST /api/collab/config` 运行时调参（约 +0.5 人日）。
11. 前端：store `characters` + assistant 双角色指令/@ 定向 + 状态面板。
12. 集成测试：双人对话多轮、互斥断言、队列优先级、回归 265。

### P3 · 增强 — 约 1-1.5 人日
13. 冷场自发闲聊（active_dialogue 角色化 + coordinator 接线）。
14. 感知彼此上下文强化（awareness 配置打磨）+ 双角色 UI 美化。

**合计约 7.5-10 人日**（不含测试约 +1 人日，测试随各阶段 TDD 进行）。

---

## 12. 验收标准（v1 完成定义）

1. 双模型同屏渲染，各自口型/表情/动作由带 role 的事件驱动。
2. 仲裁五规则均可独立单测通过（Mention/Intent/Relevance/Cooldown/Random 各含正反例）。
3. **互斥断言**：任一时刻至多一个角色发声（`current_speaker` 唯一，集成测试覆盖并发弹幕）。
4. 完整双人对话：弹幕触发主回应 → 对方在冷却达标后接话 → 形成 ≥2 轮对话（`speech:completed` 链可观测）。
5. 单角色模式零回归（评审确认的回归范围，分两层）：
   - **每阶段必测（基础层）**：`collaboration.enabled=false` 时 265 项 pytest 全绿 + 弹幕链路 demo + 既有端到端脚本（API 快照结构/四页面 200/WS 事件 seq 与断线恢复/角色切换/开关控制 37 项，即前端重构验收脚本）。约 0.5 人日/阶段。
   - **P2 完成后一次（完整层）**：追加智能助手全功能回归——状态面板（角色/开关/成本/看门狗）、开关控制、对话历史与刷新恢复（sessionStorage）、语音输入保留、消息按角色渲染。约追加 0.5 人日，仅执行一次。
6. 冷场闲聊（P3）：静默超时后双人自发对话可触发，无输入时有限频（不刷屏）。
7. 联动逻辑无真实 LLM/TTS 依赖即可测试（仲裁/话轮/触发全部 mock 化）。

## 13. 风险与兼容性

| 风险 | 缓解 |
|---|---|
| 双模型渲染性能（同一 stage 两模型） | PIXI 可承载；控制贴图尺寸/缩放；必要时分 stage 叠加 |
| 相关性误判（关键词覆盖不足） | 规则日志可观测（`speech:arbitrated` 带 rule_hit/reason）；关键词配置化可调；规则可插拔替换 |
| 接话刷屏 | `trigger_global_cooldown` + 概率约束 + 冷却规则双重限制 |
| 单角色回归 | `enabled=false` 默认关；`execute_with` 不改默认路径 |
| Lilith 模型资产缺失 | P1 前置任务：迁移旧项目模型或新制；缺失时降级为"双角色共用一模型（换色/标签区分）" |
| system_prompt 注入改变既有对话 | 注入仅在多角色模式生效；单角色模式行为不变 |

## 14. 明确不在本期范围（YAGNI）

- 第三角色（lumi）完整在场（架构可扩展，仅配置接入，不做双人以上群聊编排）。
- 角色-观众/群聊联动（复用同一套 arbitrator/triggers，另立项目）。
- 向量化相关性（留接口，本期用关键词规则）。

## 15. 评审决策摘要（2026-08-14，对应评审 6 点）

| # | 评审点 | 决策 | 落点 |
|---|---|---|---|
| 1 | 模型来源（评审调整） | **Yuki 形象 = 迁移旧项目 Hiyori**（已核验 Cubism 3 同格式同运行时，无表情文件、9 个 Idle 动作）；**Lilith = 沿用现有小恶魔**（零迁移）；Haru 留作备选；时间紧降级共用单模型换色区分 | §9.1、§11 P1 |
| 1b | Hiyori 呼吸动作限制（评审新增） | `restrict_breath: true` + `idle_silent_motion`（静默态只播低动作 idle，P1 实测选定）；前端驱动改显式状态机 SILENT/TALKING；未发言角色呼吸被压制；P3 可选参数层限制（YAGNI 兜底） | §9.1 |
| 2 | 相关性关键词来源 | character.yaml v2.1 新增 `keywords`（personality/topics/patterns，支持子串/短语/`regex:` 前缀）；缺失时从 personality/catchphrase/speaking_style 兜底推导；catchphrases.json 仅作口癖不担相关性 | §8 规则 3 |
| 3 | 接话概率/冷却初值 | `0.3`/`20.0` 为建议起步值；ConfigLoader 无热更新 → 协调器 `_runtime_config` + `POST /api/collab/config` 白名单项运行时调整，重启回落 | §8 配置 |
| 4 | 单角色回归范围 | 分两层：每阶段基础层（265 测试 + 弹幕 demo + 37 项端到端脚本，0.5 人日）；P2 后一次完整层（助手全功能 +0.5 人日） | §12 项 5 |
| 5 | 待发队列优先级 | 请求带 priority（Mention P0 > Intent P1 > Relevance P2 > Collab P3 > Active P4），插队 + 同级 FIFO；v1 不硬打断，P3 可选软抢占 | §8 互斥与排队 |
| 6 | 口型切换平滑 | 自然过渡 + 显式收尾：新角色 `lip_sync_start` 前旧模型口型归零回 idle（200ms）；后端不硬重置 | §9.1 |

总工作量由 6.5-9 人日调整为 **7.5-10 人日**（含 Hiyori 迁移、呼吸限制状态机与运行时调参 API）。
