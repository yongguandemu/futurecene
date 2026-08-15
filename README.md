# Future Scene · 智能虚拟角色自主直播系统

基于多 Agent 架构的 AI 虚拟主播全栈解决方案：让虚拟主播独立完成弹幕互动、礼物回应、游戏操作、内容策划的全流程直播工作。

**独一份亮点**：智能助手管理直播 —— AI 不只当主播，还能当直播管理员（自然语言/指令切角色、点歌、备播、查状态、管理世界书、配置排期）。

## 快速开始

### Windows（推荐）

双击 `start_demo.bat`，一键完成：依赖检查 → .env 校验 → 启动服务 → 自动打开控制台。

### 命令行

```bash
# 首次安装依赖
pip install -r requirements.txt

# 准备环境变量（复制模板并填写 API Key）
copy .env.example .env

# 启动（默认 0.0.0.0:5000，自动打开浏览器）
python run.py

# 常用参数
python run.py --no-browser   # 不自动打开浏览器
python run.py --port 8000    # 换端口
```

### Docker

```bash
docker compose up --build
```

## 启动后访问

| 地址 | 说明 |
|---|---|
| `http://127.0.0.1:5000/dashboard/` | 总控制台（直播开关 / 世界书 / 排期 / 角色配置） |
| `http://127.0.0.1:5000/worldbook/` | 世界书管理页（470+ 条设定 + 自动进化审核） |
| `http://127.0.0.1:5000/live2d/` | Live2D 直播画面源（OBS 浏览器源） |
| `http://127.0.0.1:5000/subtitle/` | 字幕叠加源（OBS 浏览器源） |
| `http://127.0.0.1:5000/api/health` | 健康检查 |

## 环境变量

必填（缺失则启动退出）：`OPENAI_API_KEY`（LLM pro 引擎）、`ZHIPU_API_KEY`（LLM fast 引擎）、`DASHSCOPE_API_KEY`（TTS/视觉）、`WUSOUND_API_KEY`（TTS）、`OBS_WS_PASSWORD`（OBS 控制）。

延后（缺失仅警告）：`BILIBILI_ACCESS_KEY_ID/SECRET/COOKIE`（接入 B站直播时补齐）。

模板见 `.env.example`，密钥只从环境变量/`.env` 读取，禁止写入配置文件。

## 双引擎路由

| 引擎 | 模型 | 场景 | 成本 |
|---|---|---|---|
| fast | GLM-4.7-FlashX（智谱） | 弹幕对话 / 主动话题 | 极速低价 |
| pro | DeepSeek V4 Pro | 游戏规划 / 解说 / 仲裁 | 按需闲时价 |

月运营成本（每周 6 场 × 4h 直播）约 ¥93，详见商业计划书。

## 测试

```bash
python -m pytest tests -q     # 全量（540+ 用例）
```

## 目录结构

```
src/
  app.py                  # 系统装配（build_app_context）
  commander/              # 指挥官：意图解析 / 命令分发 / 决策分级 / 工具调用
  orchestrators/          # 14 个调度官（llm/tts/live2d/记忆/安全/弹幕/音乐/平台/推流/经验/日程…）
  shared/                 # 事件总线 / 配置 / 决策日志 / 世界书
  web/                    # Flask 应用工厂 + REST/WS 路由
frontend/                 # 总控制台 / Live2D 源 / 字幕叠加 / 世界书管理
config/                   # config.yaml + 角色档案（yuki / lilith）
scripts/demo_danmaku.py   # 弹幕演示脚本
docs/                     # 设计规格 / 升级路线图
```

## 说明

- 本仓库为项目代码与演示资产；商业计划书 PPT 与角色立绘素材不在版本控制内。
- B站直播需先开通直播间，填入推流配置（`config.yaml` → `stream` 段）与 `.env` B站凭据。
