# System Prompt 审计与整改实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 executing-plans 按任务顺序执行。步骤用 checkbox（`- [ ]`）跟踪。

**Goal:** 定位系统中全部 system prompt，按质量评分分组（100-90 直接过 / 90-50 模拟输出测试 / 0-50 重写后测试），低分项整改后复测，交付审计报告。

**Architecture:** 审计对象 = 代码内 prompt 常量 + 角色配置文件 system_prompt.txt + 动态注入块（世界书/工具/感知）。评分维度：输出结构约束是否明确（JSON schema）、内容是否足够详细可靠、与下游解析器数据结构是否合规、鲁棒性（兜底/示例）。模拟测试 = 独立脚本用真实 LLM 生成输出，做格式校验与内容抽查。

**Tech Stack:** Python、现有 llm 调度官（OpenAI 兼容客户端）、pytest、JSON schema 校验（手写）。

---

## 审计清单（16 个有效 prompt + 1 个停用）

| # | 名称 | 位置 | 类型 |
|---|------|------|------|
| P1 | Yuki 角色人设 | config/profiles/yuki/system_prompt.txt | 人格 |
| P2 | Lilith 角色人设 | config/profiles/lilith/system_prompt.txt | 人格 |
| P3 | 系统能力说明 | src/commander/command_router.py:37-46 `_SYSTEM_CAPABILITIES` | 身份/能力 |
| P4 | 智能助手中立身份 | src/commander/command_router.py:50-55 `_ASSISTANT_NOTICE` | 身份/能力 |
| P5 | 角色定向说明 | src/commander/command_router.py:58-62 `_ROLE_NOTICE` | 身份/能力 |
| P6 | 世界书注入块 | src/shared/world_book.py:228-245 `system_prompt_block` | 动态注入 |
| P7 | 工具清单块 | src/commander/tool_registry.py:74-82 `prompt_block` | 动态注入 |
| P8 | 协作发言权判断 | src/orchestrators/collaboration/judge.py:104-108 `_JUDGE_SYSTEM` | 结构化输出 |
| P9 | 协作感知彼此 | src/orchestrators/collaboration/context_manager.py:77-85 `build_system_prompt` | 动态注入 |
| P10 | 主动话题生成 | src/app.py:108-111（角色化话题 prompt） | 文本生成 |
| P11 | 批量发言策划 | src/orchestrators/llm_orchestrator/batch_planner.py:56-65 `_PROMPT_TEMPLATE` | 结构化输出 |
| P12 | 记忆压缩摘要 | src/app.py:256-259 `_memory_summarize` system_prompt | 文本生成 |
| P13 | 游戏操作规划 | src/orchestrators/game_orchestrator/game_operation_planner.py:82-93 `_SYSTEM_PROMPT` | 结构化输出 |
| P14 | 经验学习操作建议 | src/orchestrators/experience_orchestrator/learn_brain.py:464-468 | 结构化输出 |
| P15 | 经验学习失败修正 | src/orchestrators/experience_orchestrator/learn_brain.py:591-594 | 结构化输出 |
| P16 | MC 任务拆解 | src/orchestrators/experience_orchestrator/task_planner.py:149-151 | 结构化输出 |
| P17 | Lumi 角色人设 | config/profiles/lumi/system_prompt.txt | 停用占位，不参与 |

## 初始评分与分组

| # | 评分 | 分组 | 主要依据 |
|---|------|------|---------|
| P13 | 92 | 100-90 直接过 | 操作类型枚举 + 参数 + 示例 + 失败返回 []，完整 |
| P3 | 90 | 90-50 模拟测试 | 真实能力 + 示例 + 反虚构；缺输出格式 |
| P4 | 90 | 90-50 模拟测试 | 中立身份明确；缺回复格式指引 |
| P12 | 90 | 90-50 模拟测试 | 任务清晰、字数约束、保留要素明确 |
| P5 | 88 | 90-50 模拟测试 | 模板化，依赖角色人设；缺互动边界 |
| P7 | 88 | 90-50 模拟测试 | 调用格式清晰 |
| P11 | 88 | 90-50 模拟测试 | JSON schema 完整；mood 枚举缺示例 |
| P6 | 85 | 90-50 模拟测试 | 数据块清晰；缺"如何运用"指引 |
| P10 | 82 | 90-50 模拟测试 | 任务+约束清晰；无输出格式约束 |
| P14 | 78 | 90-50 模拟测试 | 动作枚举+示例；拼装随意、无严格格式声明 |
| P8 | 75 | 90-50 模拟测试 | JSON schema 有；缺示例、缺打分依据 |
| P15 | 72 | 90-50 模拟测试 | 有示例；过于简略 |
| P16 | 70 | 90-50 模拟测试 | 行格式说明；无示例、解析器脆弱 |
| P9 | 60 | 90-50 模拟测试 | 极简拼接；缺利用指引 |
| P1 | 55 | 90-50 模拟测试 | 内容单薄，缺互动边界/长度/背景 |
| P2 | 55 | 90-50 模拟测试 | 同上 |

无 0-50 初始项；模拟测试发现问题则升级处置。

---

### Task 1: 审计报告文档落盘

**Files:**
- Create: `docs/prompts/system-prompt-audit.md`（评分表 + 分组 + 处置记录，随整改更新）

- [ ] **Step 1:** 写入初始审计报告（上表 + 完整 prompt 原文摘录 + 评分依据）

### Task 2: 模拟输出测试脚本

**Files:**
- Create: `scripts/prompt_audit.py`

- [ ] **Step 1:** 写脚本：读取各 prompt 来源（角色 txt / 代码常量 / 动态块），构造代表性输入，经真实 LLM（llm 调度官 fast 引擎）生成输出
- [ ] **Step 2:** 输出校验：JSON 类（P8/P11/P13/P14/P15/P16）做 schema/解析校验；文本类（P1-P7/P9/P10/P12）做长度/语气/要素抽查
- [ ] **Step 3:** 结果写 `data/audit/prompt_audit_results.json`（不落临时目录，供报告引用）

### Task 3: 运行模拟测试（90-50 全部分组）

- [ ] **Step 1:** 服务运行状态下执行 `python scripts/prompt_audit.py`
- [ ] **Step 2:** 逐项记录：输出结构是否合规、内容是否足够详细可靠、失败原因
- [ ] **Step 3:** 汇总问题清单 → 更新审计报告

### Task 4: 重写/修改问题 prompt

**Files:**
- Modify: `config/profiles/yuki/system_prompt.txt`、`config/profiles/lilith/system_prompt.txt`（若测试暴露单薄）
- Modify: `src/orchestrators/collaboration/judge.py:104-108`、`src/orchestrators/experience_orchestrator/learn_brain.py`、`src/orchestrators/experience_orchestrator/task_planner.py`、`src/orchestrators/collaboration/context_manager.py`、`src/commander/command_router.py`、`src/app.py`（按测试结果）

- [ ] **Step 1:** 按问题清单逐项修改（补示例、补 schema、补边界、补利用指引）
- [ ] **Step 2:** 每项改动同步更新 8 项契约 docstring（如涉及）
- [ ] **Step 3:** 新增/更新对应单测（prompt 格式断言）

### Task 5: 复测与回归

- [ ] **Step 1:** 重跑 `python scripts/prompt_audit.py`，确认问题项通过
- [ ] **Step 2:** 全量 `python -m pytest tests -q` 全绿
- [ ] **Step 3:** 更新审计报告终版（评分→重评 + 处置记录）
- [ ] **Step 4:** 提交（Conventional Commits，本地）
