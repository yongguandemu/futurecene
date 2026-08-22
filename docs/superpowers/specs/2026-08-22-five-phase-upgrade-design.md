# Future Scene V2 五阶段升级设计（2026-08-22）

> 状态：**Approved（2026-08-22 用户拍板 4 项 QA，按推荐执行）**
> 范围：任务一~五（信息分发 / 批量问询 / Live2D 本地驱动 / 分层记忆 / 前端补全）
> 依据：用户口述需求 + 现状代码勘察（commander / orchestrators / shared / web / frontend）

## 0.1 QA 决策记录（已确认）
| # | 讨论点 | 决策 |
|---|--------|------|
| Q1 | 总控落点 | 指挥官层内新增 input 域（不建第二大脑） |
| Q2 | 自循环输入范围 | 发言文本回流仅「明确意图」才动作，无意图归档短期记忆 |
| Q3 | 主动发言 TTS 合成时机 | 播放前按需合成（窗口 1 条），过期丢弃不浪费合成成本 |
| Q4 | 记忆系统落点 | 扩展现有 memory_orchestrator（L1=现有 short_term、L3=现有 long_term 升级） |
| Q5 | 情绪模型 | 规则兜底 + ONNX 接口预留（模型文件后置） |
| Q6 | 30fps 参数 | 后端 10Hz 参数帧 + 前端 30fps 插值 |
| Q7 | 口型时间戳 | 振幅驱动 + 音素时间戳接口预留 |
| Q8 | 弹幕二分 | 询问型→被动、话题型→主动素材 |

## 0. 总览

五任务构成一次「总控调度化 + 表达自主化 + 记忆分层化 + 运营可视化的整体升级」。
依赖链（用户给定）：一 + 三 并行 → 二 → 四 → 五。

全局约束（所有任务适用）：
- 新模块 docstring 必须含完整 8 项契约（AGENTS.md 模板）
- 六大纪律 D1-D6：总控归指挥官唯一、调度官不互 import、事件必须注册 ALL_EVENTS、开关集中 switch_manager、注册表驱动扩展
- 新事件：`src/shared/events.py` 定义常量 + 收录 `ALL_EVENTS`（有 `test_events_schema.py` 校验）
- 新开关：`SwitchManager.auto_register`（调度官注册时自动生成）或显式注册
- 测试：新模块配套单测；每阶段结束全量 pytest 全绿 + 冒烟（大型修改必跑完整冒烟）
- 推送门禁：未经用户确认不 push，提交只留本地

---

## 1. 任务一：信息输入分层分发 + 总控调度化改造

### 1.1 目标
五种输入统一分类/排队/分发，总控从「被动响应」变为「主动调度」；
系统输出可作为新输入回流（自循环），最终实现 AI「边说边玩、自话自驱」。

### 1.2 五类输入定义与处置
| 类型 | 来源 | 优先级 | 处置 |
|------|------|--------|------|
| operator（操作者） | 用户「!指令」/ 前端命令 | P0 最高 | 绕过队列直入总控，带身份标记，可跳过循环深度限制 |
| audience（观众） | 弹幕 / 礼物 / 小游戏 | P1 | 进队列，按子类型路由（回复/点歌/游戏互动） |
| external_app（外部应用） | 屏幕控制反馈 / 实况状态变化 | P2 | 进队列，低延迟直通对应域 |
| system_loop（系统循环） | 系统输出回流（发言/操作结果） | P3 最低 | 进队列，带循环深度标记（上限 5），超限归档短期记忆不触发分发 |
| reference（既定资料） | 长期记忆 / 世界书 / 直播脚本 / 批量问询结果 | 不排队 | 走 ContextAggregator 作为上下文，不产生响应请求 |

### 1.3 架构落点（推荐：commander 层内新增 input 域）
```
src/commander/input/
├── input_classifier.py     # InputClassifier：五类识别 + 优先级标签 + operator 身份标记
├── priority_queue.py       # PriorityQueue：按优先级排序；operator 直插队首
├── distribution_router.py  # DistributionRouter：类型/状态 → 目标域（复用 orchestrator_registry 能力映射）
└── context_aggregator.py   # ContextAggregator：短期记忆 + reference 资料 + 当前状态 → 上下文快照
```
- 短期记忆（全模块接收信息备份，循环缓冲区）：**扩展现有 `memory_orchestrator/short_term.py`**（不新建模块），由 EventLogger（任务四）统一写入。
- 总控 = 现有指挥官层升级：`danmaku_pipeline` / `command_router` 的入口统一改为「InputClassifier → PriorityQueue → DistributionRouter」的调度链路，原链路作为 direct 模式保留。

### 1.4 总控分发模式开关
`switch_manager.auto_register("input_dispatch", default=False)`，三模式：
- direct（直通，默认关闭时行为=现状）：各输入走原链路（弹幕→pipeline、命令→router）
- priority（优先级）：全部输入进 PriorityQueue 按序分发（总控调度）
- adaptive（自适应）：priority 基础上按上下文（冷场/热点/协作状态）动态调权

### 1.5 自循环规则
- 系统输出（LLM 发言文本 / 操作结果反馈）→ `input:classified` 标 `source=system_loop` + `loop_depth=N`（N 每回流 +1）
- 发言文本回流经 `intent_parser` 解析：命中明确意图（如「我去捡装备」→ 游戏操作）才分发执行；无意图 → 归档短期记忆
- `loop_depth > 5` → 直接归档短期记忆，不触发分发
- operator 输入：`loop_depth` 不计数，永远可分发

### 1.6 事件与开关
- 新事件：`input:classified`（类型/优先级/身份标记/loop_depth）、`input:queued`、`input:routed`
- `context:snapshot` 复用已有 `CONTEXT_SNAPSHOT_READY`（不重复建）

### 1.7 测试
- 分类器：五类 + 边界（空文本、`!`前缀、@定向）
- 队列：优先级排序、operator 插队、深度超限归档
- 路由：direct 模式回归（现有 pipeline/router 测试全绿）、priority 模式新链路
- 自循环：深度递增、超限不再分发、operator 跳过限制

### 1.8 风险与对策
- 改造入口影响所有链路 → 分两步：先加 input 域（旁路），再切入口；direct 模式保证可回退
- 自循环发散 → 仅「明确意图」才动作 + 深度上限 + 无意图即归档

---

## 2. 任务二：批量问询方案（主动/被动发言分离）

### 2.1 目标
主动发言按「批」预生成（几分钟的发言计划），被动发言实时插入空档；降低 LLM 调用频次与延迟。

### 2.2 主动/被动界定
| 类型 | 内容 | 特征 |
|------|------|------|
| 主动 | 话题持续发言、介绍、双角色互聊（与极短期风向无关） | 稀疏、可预生成、可过期 |
| 被动 | 弹幕询问型回复、突发事件回应、实时解说 | 即时、依赖当前事件、可插空档 |
- 弹幕二分：询问型 → 被动；话题型 → 主动素材（BatchPlanner 参考）

### 2.3 架构落点
```
src/orchestrators/llm_orchestrator/batch_planner.py   # BatchPlanner：LLM 批量生成发言计划
src/commander/speech_scheduler.py                     # SpeechScheduler：发言时间线调度（指挥官层，协调字幕/TTS/仲裁）
src/orchestrators/tts_orchestrator/tts_preprocessor.py # TTS Preprocessor：清洗 + 情感参数映射
```
- BatchPlanner 输出：`[{text, mood, suggested_window_sec, duration_estimate}]`（LLM 用稳定模型 deepseek-v4-flash）
- ActiveDialogue：保留触发框架（冷场检测），生成逻辑替换为调 BatchPlanner
- SpeechScheduler：主动发言队列 + 空档管理；被动发言请求插队到当前空档；超时/覆盖丢弃

### 2.4 过期与丢弃
- 预生成条目超过 2-3 分钟未播放 → 丢弃
- 被被动发言覆盖 ≥3 次 → 丢弃
- 建议时间窗不强制绑定（软约束）

### 2.5 TTS 合成时机（讨论点，推荐播放前合成）
- 推荐：SpeechScheduler 决定「即将播放」（窗口 1 条）时才调 TTS 合成，避免过期丢弃浪费合成成本
- 备选（用户原述）：TTS 预生成队列按序合成——仅对确定会播的批启用

### 2.6 事件与开关
- 新事件：`speech:batch_ready`（批量计划就绪）、`speech:scheduled`（发言已排期）、`speech:inserted`（被动插入）
- 复用已有：`SPEECH_ENQUEUED/DEQUEUED`、`SPEECH_COMPLETED`
- 新开关：`batch_mode`（默认 off）、`real_time_mode`（默认 on）

### 2.7 与协作域衔接
- 多角色时主动批发言同样过 `collab:*` 发言权仲裁；被动插入不抢占已获权的发言（排队空档）

### 2.8 测试
- BatchPlanner：结构化输出 schema、模型降级（deepseek-v4-flash 不可用 → 单条回退）
- SpeechScheduler：队列/空档/过期丢弃/覆盖丢弃/插入不抢占
- TTS Preprocessor：清洗文本、情感参数映射、空输入

---

## 3. 任务三：Live2D 本地模型驱动（情绪+动作+口型）

### 3.1 目标
呼吸禁用（保留眨眼/头发/身体轻微起伏）；本地模型驱动情绪/动作/参数；与 TTS 时间点联动；
规则方案兜底；本地推理唯一路径（云端延迟不可接受）。

### 3.2 架构落点
```
src/orchestrators/live2d_orchestrator/
├── parameter_registry.py  # ParameterRegistry：解析 data/models/*/*.model3.json 的 Parameters（ID/范围/默认值）缓存
├── emotion_extractor.py   # EmotionExtractor：文本 → 情绪标签（本地）
├── motion_scheduler.py    # MotionScheduler：情绪+状态 → 身体动作决策
├── parameter_mapper.py    # ParameterMapper：情绪+动作 → 具体参数值范围
└── timing_controller.py   # TimingController：口型/呼吸/动作时序协调
```
- 情绪提取：轻量 ONNX 模型（tiny-emotion 类，模型文件后续放入 `data/models/emotion/`）；模型未就绪 → 规则兜底（情绪词典 + 语气词/标点）
- 口型：优先 TTS 音素时间戳（Wusound 实测大概率不支持 → 预留接口）；当前用音频振幅驱动（前端按 audio 文件分帧 RMS）+ duration_ms
- 非发言时段：身体正弦起伏（idle 参数）、周期性眨眼（3-5s）、头发物理（Live2D 自带）

### 3.3 参数推送通道（30fps）
- 后端不推 30 个事件/秒（事件总线不适合高频）；改为 **10Hz 批量参数帧事件 + 前端 30fps 插值渲染**
- 新事件：`emotion:extracted`（低频）、`motion:triggered`（低频）；高频参数走 `live2d:params_batch`（10Hz 聚合）或复用 `FRONTEND_STATUS_UPDATE` 扩展

### 3.4 修改点
- Live2DOrchestrator：新增 `live2d:params_update` 能力（批量参数帧）；口型结束线程保留
- TTSOrchestrator：预留 `tts:phoneme_timestamps` 接口（有则输出，无则 None）
- 前端 live2d_actor.js：新增参数帧消费 + 插值；呼吸禁用已实现（restrictBreath）

### 3.5 事件与开关
- 新事件：`emotion:extracted`、`motion:triggered`、`live2d:params_batch`
- 新开关：`live2d_emotion_model`（本地模型 vs 规则兜底，默认规则兜底）

### 3.6 测试
- ParameterRegistry：model3.json 解析（ID/范围/默认值）、文件缺失兜底
- EmotionExtractor：规则兜底准确率冒烟、模型加载降级
- ParameterMapper：情绪→参数范围映射边界（无效情绪回退平静）
- TimingController：时序（口型期间不插入动作冲突）
- 前端插值：30fps 渲染、10Hz 输入无跳变

---

## 4. 任务四：分层记忆系统（L0-L3）

### 4.1 目标
记录「说了/听了/做了/看了」全量事件；自然语言存储；文本量触发压缩；检索不调 LLM（纯文本+向量）。

### 4.2 架构落点（推荐：扩展现有 memory_orchestrator，不新建调度官）
```
src/orchestrators/memory_orchestrator/
├── event_logger.py     # EventLogger：订阅全量 EventBus 事件 → L0 落盘 + L1 循环缓冲
├── memory_compressor.py# MemoryCompressor：异步压缩（L1→L2 摘要、L2→L3 归档）
├── memory_config.py    # MemoryConfig：保留期/压缩阈值/模型选择/记忆强度
└── review.py           # MemoryReview：L3→世界书改动提案审批流（任务五消费）
```
- L0 原始事件日志：`data/memory/l0/YYYYMMDD.jsonl`，2-4 周按时间清理（不参与检索）
- L1 短期记忆：扩展现有 `short_term.py` → 全量事件循环缓冲 + 时间窗（5-10 分钟）
- L2 中期记忆：SQLite 表 `memory_l2`（每段 500-1000 字摘要），数周-数月
- L3 长期记忆：扩展现有 `long_term.py`（每段 100-300 字），永久；向量检索复用 `retriever.py`（numpy n-gram，无需外部向量库）
- 压缩触发：累计 3000-5000 字 → `memory_compressor` 异步摘要；模型统一 deepseek-v4-flash（禁用 glm-4.7-flash）

### 4.3 检索与记忆强度
- MemoryRetriever（扩展现有 retriever）：L1 时间窗 + L2/L3 摘要混合检索，纯文本 + 向量，不调 LLM
- 记忆强度（任务五设置）：低 2 / 中 5 / 高 10 / 超强 15 条（retrieve k + 调用频率节流）

### 4.4 "做了什么/看了什么"采集
- EventLogger 订阅现成事件即可：screen（`SCREEN_CURSOR_*`）、live2d（`LIVE2D_*`）、game（`GAME_*`）、speech（`SPEECH_*`）——**不需要各模块改日志**

### 4.5 事件与开关
- 新事件：`memory:event_logged`（L0 落盘完成）
- 新开关：`memory_compression`（默认 on）、`memory_strength` 读 ConfigLoader（前端设置）

### 4.6 测试
- EventLogger：事件写入 JSONL + L1 缓冲、时间窗淘汰
- MemoryCompressor：阈值触发、摘要落 L2、异常降级（模型不可用 → 保留原文分段）
- 检索：L1/L2/L3 混合、记忆强度 k 映射
- 不影响现有功能：现有 `memory:store/retrieve/consolidate` 行为不变

---

## 5. 任务五：前端补全项（设置面板 + 用量监控 + 角色配置 + 直播间视图）

### 5.1 目标
运营可读的设置/用量/角色配置/直播间预览；记忆与世界书审阅流持久化。

### 5.2 设置面板（/api/config）
- 设置项（ConfigLoader 持久化 + 新路由读写；用户定稿：保留 4 项 + 语义细化，直播节奏等新项不设计）：
  - `memory_strength`：low/medium/high/ultra → MemoryRetriever k（2/5/10/15）与检索频率
  - `tts_output_target`：local（本机扬声器）/ stream（直播间推流）/ both（两者）——替代单布尔开关
  - `allow_memory_to_worldbook`：默认 off（关=不生成 L3→世界书提案；开=生成提案走 MemoryReview 人工审阅，非直接写入）
  - `reasoning_intensity`：省电（flash、短回复）/ 标准（flash、正常）/ 增强（pro、长回复、低温度）——映射 `engine + max_tokens + temperature`，不碰记忆压缩等后台任务
- 新路由：`GET/PUT /api/config`（web/routes/config.py）

### 5.3 用量监控（/api/metrics）
- CostTracker 新增 `get_stats(period="today")`（按日聚合 by_type/cost/calls/tokens）
- 补齐 `/api/metrics` 路由 + dashboard 用量视图

### 5.4 角色配置（世界书 + 记忆库 + 审阅）
- 世界书条目查看：复用 `/api/worldbook`（已有 CRUD）
- 记忆库查看：新路由 `/api/memory`（L0 概览 / L1 时间窗 / L2/L3 检索）
- 重要记事字段：worldbook entry `metadata.priority_note=true`（用户审阅时决定，非自动）
- MemoryReview 审批流：L3→世界书提案 → 待审阅列表 → 接受/驳回 → 持久化（`data/memory_review.json`），登录可见可处理
- 新路由：`/api/memory/review`（GET 列表 / POST 接受|驳回）

### 5.5 直播间视图
- dashboard 新视图（iframe 复用 live2d_stream 渲染引擎，与录播纯净视图共享）
- 组件可拖拽布局（形象/弹幕框/字幕框），布局配置存 `/api/config`（或 localStorage）
- 纯前端组件，不新增后端渲染能力

### 5.6 测试
- 后端：config 读写、metrics 聚合（today 边界）、memory review 持久化、worldbook 重要记事字段
- 前端：设置读写生效、用量展示、审阅流操作、拖拽布局持久化（手工冒烟）

---

## 6. 关键讨论点（QA 项）

| # | 讨论点 | 我的推荐 | 影响 |
|---|--------|----------|------|
| Q1 | 总控落点 | commander 层内新增 input 域（不建第二大脑，守 D1） | 任务一架构 |
| Q2 | 自循环输入范围 | 发言文本回流仅「明确意图」才动作 + 操作反馈；无意图归档 | 任务一防发散 |
| Q3 | 主动发言 TTS 合成时机 | 播放前合成（窗口 1 条），避免过期丢弃浪费 | 任务二成本/延迟 |
| Q4 | 记忆系统落点 | 扩展现有 memory_orchestrator（L1=现有 short_term、L3=现有 long_term 升级） | 任务四改动面 |
| Q5 | 情绪模型 | 先规则兜底 + ONNX 接口预留（tiny-emotion），模型文件后置 | 任务三依赖 |
| Q6 | 30fps 参数 | 后端 10Hz 参数帧 + 前端 30fps 插值 | 任务三通道 |
| Q7 | 口型时间戳 | Wusound 大概率无音素时间戳 → 振幅驱动 + 接口预留 | 任务三现实约束 |
| Q8 | 弹幕二分 | 询问型→被动、话题型→主动素材，由分类器给 hint | 任务二/一衔接 |

## 7. 执行计划

| 阶段 | 内容 | 出口条件 |
|------|------|----------|
| P0 | 本方案确认（用户拍板 QA 项） | 用户确认 |
| P1 | 任务一（input 域）+ 任务三（live2d 参数驱动）并行 | 单测 + 全量 pytest 全绿 + 冒烟 |
| P2 | 任务二（批量问询） | 同上 |
| P3 | 任务四（记忆分层） | 同上 |
| P4 | 任务五（前端补全） | 同上 + 前端手工冒烟 |

每阶段结束：向用户汇报关键内容 → 用户确认 → 提交（不推送，推送门禁）。
