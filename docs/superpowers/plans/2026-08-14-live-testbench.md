# 聊天直播测试台实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 升级 `live2d_stream` 为完整直播测试台：弹幕注入+显示、LLM 回复（主动+被动）、TTS 出声、Live2D 口型/表情/动作、直播间背景装饰。

**Architecture:** 后端新增 `/api/danmaku` 注入端点、启动 ActiveDialogue 并接线主动发言、装配 `live2d:load`；前端 live2d_stream 增弹幕输入/显示框、TTS 播放器、背景装饰层。所有事件走既有 EventBus + WS 广播。

**Tech Stack:** Flask + EventBus（后端）、原生 JS + PixiJS（前端）。

**Spec:** `docs/superpowers/specs/2026-08-14-live-testbench-design.md`

---

## 文件结构

```
src/web/routes/danmaku.py            创建：POST /api/danmaku 测试弹幕注入
src/web/routes/__init__.py           修改：注册 danmaku blueprint
src/web/app_factory.py               修改：注册 danmaku blueprint
src/app.py                           修改：启动 ActiveDialogue + 装配 live2d:load
src/commander/danmaku_pipeline.py    修改：增加 _speak_active（主动发言走字幕+TTS）
tests/test_danmaku_api.py            创建：/api/danmaku 发布事件断言
frontend/live2d_stream/index.html    修改：弹幕输入/显示框 + TTS 播放器 + 背景装饰
```

---

### Task 1: POST /api/danmaku 测试弹幕注入端点

**Files:**
- Create: `src/web/routes/danmaku.py`
- Modify: `src/web/routes/__init__.py`、`src/web/app_factory.py`
- Test: `tests/test_danmaku_api.py`

- [ ] **Step 1: 写失败测试**（创建 `tests/test_danmaku_api.py`）

```python
"""test_danmaku_api.py — POST /api/danmaku 测试弹幕注入（直播测试台）"""
from tests.test_web_routes import _make_context
from src.shared.events import DANMAKU_RECEIVED
from src.web.app_factory import create_app


def test_danmaku_api_publishes_event():
    ctx = _make_context()
    app = create_app(ctx)
    client = app.test_client()
    seen = []
    ctx["event_bus"].subscribe(DANMAKU_RECEIVED,
                               lambda event, **kw: seen.append(kw))
    resp = client.post("/api/danmaku",
                       json={"content": "你好呀", "user_name": "测试观众"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "command_id" in data
    assert len(seen) == 1
    assert seen[0]["content"] == "你好呀"
    assert seen[0]["user_name"] == "测试观众"


def test_danmaku_api_missing_content_400():
    ctx = _make_context()
    app = create_app(ctx)
    resp = app.test_client().post("/api/danmaku", json={})
    assert resp.status_code == 400
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_danmaku_api.py -v`
Expected: FAIL（404，路由不存在）

- [ ] **Step 3: 实现端点**

创建 `src/web/routes/danmaku.py`：

```python
"""routes/danmaku.py — POST /api/danmaku（直播测试台 · 模拟弹幕注入）

测试用弹幕入口：前端直播测试台输入 → 发布 danmaku:received → 走完整链路
（安全过滤 → LLM → 字幕 → TTS → Live2D 口型）。

# 模块内容清单（8 项契约）
1. 模块身份标识：web.routes · danmaku · POST /api/danmaku
2. 配置契约：从 current_app.config["APP_CONTEXT"] 取 event_bus
3. 输入契约：JSON body {content 必填、user_name 可选、user_id 可选}
4. 输出契约：{ok, command_id}；发布 danmaku:received（与 normalizer 同构 payload）
5. 依赖声明：flask（Blueprint/current_app/jsonify/request）、uuid、shared.events
6. 错误定义：event_bus 未装配 503；content 缺失 400
7. 生命周期方法：无（Blueprint 路由函数 inject_danmaku()）
8. 领域状态说明：无模块级可变状态；依赖 APP_CONTEXT 注入的 event_bus
"""
import logging
import uuid

from flask import Blueprint, current_app, jsonify, request

from src.shared.events import DANMAKU_RECEIVED

logger = logging.getLogger(__name__)

bp = Blueprint("danmaku", __name__, url_prefix="/api")


@bp.route("/danmaku", methods=["POST"])
def inject_danmaku():
    context = current_app.config.get("APP_CONTEXT", {})
    event_bus = context.get("event_bus")
    if event_bus is None:
        return jsonify({"ok": False, "error": "EventBus 未装配"}), 503

    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"ok": False, "error": "content 必填"}), 400

    command_id = uuid.uuid4().hex
    event_bus.publish(
        DANMAKU_RECEIVED,
        event_type="danmaku",
        content=content,
        user_name=data.get("user_name") or "测试观众",
        user_id=data.get("user_id") or "test-user",
        extra={},
        timestamp=0.0,
    )
    return jsonify({"ok": True, "command_id": command_id})
```

- [ ] **Step 4: 注册 blueprint**

`src/web/routes/__init__.py` 导出 danmaku；`src/web/app_factory.py` 注册：

```python
from src.web.routes.danmaku import bp as danmaku_bp
# 与其他 bp 一同 register_blueprint
```

- [ ] **Step 5: 运行确认通过**

Run: `python -m pytest tests/test_danmaku_api.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add src/web/routes/danmaku.py src/web/routes/__init__.py src/web/app_factory.py tests/test_danmaku_api.py
git commit -m "feat(api): POST /api/danmaku 测试弹幕注入端点"
```

---

### Task 2: 启用主动对话 + 装配 live2d:load

**Files:**
- Modify: `src/app.py`、`src/commander/danmaku_pipeline.py`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_danmaku_pipeline.py`）

```python
def test_active_speaker_publishes_subtitle_and_tts():
    """dialogue:active → 主动发言（字幕 + TTS 调用，直播测试台主动模式）。"""
    from src.shared.events import ACTIVE_DIALOGUE
    bus = EventBus()
    bus.reset()
    llm = FakeLLM(reply="主动说话内容")
    tts = FakeTTS()
    pipe = DanmakuPipeline(event_bus=bus, llm_orchestrator=llm,
                           tts_orchestrator=tts)
    seen = {}
    bus.subscribe(FRONTEND_SUBTITLE_UPDATE, lambda event, **kw: seen.update(kw))
    pipe.start()
    bus.publish(ACTIVE_DIALOGUE, text="今天播点什么好呢",
                mood="default", role="yuki", timestamp=0.0)
    assert seen.get("text") == "今天播点什么好呢"
    assert tts.calls and tts.calls[0]["capability"] in ("tts:synthesize", "tts:stream_synthesize")
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_danmaku_pipeline.py::test_active_speaker_publishes_subtitle_and_tts -v`
Expected: FAIL（无订阅者，seen 为空 / tts.calls 空）

- [ ] **Step 3: DanmakuPipeline 增加主动发言**

在 `danmaku_pipeline.py` 的 `start()` 中追加订阅，并新增方法：

```python
def start(self) -> None:
    if self._started:
        return
    self._event_bus.subscribe(DANMAKU_RECEIVED, self._on_danmaku)
    self._event_bus.subscribe(ACTIVE_DIALOGUE, self._on_active_dialogue)
    self._started = True

def _on_active_dialogue(self, event: str, text: str = "", **kwargs) -> None:
    """冷场主动发言：字幕 + TTS 合成（Live2D 口型由 audio_ready 驱动）。"""
    text = (text or "").strip()
    if not text:
        return
    try:
        asyncio.run(self._speak_active(text))
    except Exception as e:
        logger.error("[DanmakuPipeline] 主动发言异常: %s", e)

async def _speak_active(self, text: str) -> None:
    self._event_bus.publish(FRONTEND_SUBTITLE_UPDATE,
                            text=text, role=self._current_role())
    await self._synthesize(text)
```

（`stop()` 同步取消订阅 ACTIVE_DIALOGUE。）

- [ ] **Step 4: app.py 装配**

在 `app.py` 的 pipeline.start() 之后追加：

```python
    # ---------- 冷场主动对话（直播测试台 · 主动模式） ----------
    active_dialogue = getattr(llm_orch, "_active", None)
    if active_dialogue is not None:
        active_dialogue.set_event_bus(event_bus)
        active_dialogue.start()
        logger.info("[app] ActiveDialogue 主动对话已启动")

    # ---------- Live2D 模型装载（直播测试台：使后端模型状态非空） ----------
    live2d_orch = registry.get("live2d")
    if live2d_orch is not None:
        try:
            load_result = live2d_orch.handle(
                {"capability": "live2d:load",
                 "payload": {"role": session.role}})
            logger.info("[app] live2d:load 完成: %s", load_result)
        except Exception as e:
            logger.warning("[app] live2d:load 失败: %s", e)
```

- [ ] **Step 5: 运行确认通过**

Run: `python -m pytest tests/test_danmaku_pipeline.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add src/app.py src/commander/danmaku_pipeline.py tests/test_danmaku_pipeline.py
git commit -m "feat(active): 启用主动对话 + 装配 live2d:load"
```

---

### Task 3: live2d_stream 前端升级（弹幕输入/显示 + TTS 播放 + 背景）

**Files:**
- Modify: `frontend/live2d_stream/index.html`

- [ ] **Step 1: 添加 HTML 结构**（body 内 canvas-holder 之后）

```html
    <!-- 直播间背景装饰层（assets/streaming 素材） -->
    <div id="stream-deco">
      <img src="/assets/streaming/banner_top.png" alt="" class="deco-banner-top">
      <img src="/assets/streaming/bar_bottom.png" alt="" class="deco-bar-bottom">
      <img src="/assets/streaming/panel_danmu.png" alt="" class="deco-panel-danmu">
    </div>

    <!-- 弹幕显示框（测试用） -->
    <div id="danmu-panel">
      <div id="danmu-list"></div>
    </div>

    <!-- 弹幕输入框（测试用） -->
    <div id="danmu-input-bar">
      <input id="danmuInput" type="text" placeholder="输入测试弹幕，回车发送…">
      <button id="danmuSend">发送弹幕</button>
    </div>
```

- [ ] **Step 2: CSS**

```css
#stream-deco { position: fixed; inset: 0; pointer-events: none; z-index: 1; }
#stream-deco img { position: absolute; }
.deco-banner-top { top: 0; left: 0; width: 100%; }
.deco-bar-bottom { bottom: 0; left: 0; width: 100%; }
.deco-panel-danmu { right: 20px; top: 20px; width: 320px; opacity: 0.5; }
#danmu-panel {
  position: fixed; right: 20px; bottom: 90px; width: 320px; max-height: 40vh;
  overflow-y: auto; z-index: 2; background: rgba(0,0,0,0.35);
  border-radius: 8px; padding: 8px; display: flex; flex-direction: column-reverse;
}
#danmu-list .dm-item { font-size: 13px; color: #fff; margin: 4px 0; word-break: break-all; }
#danmu-list .dm-item .dm-user { color: #ffd666; margin-right: 6px; }
#danmu-list .dm-item.ai { color: #9be8ff; }
#danmu-list .dm-item.filtered { color: #ff6b6b; text-decoration: line-through; }
#danmu-input-bar {
  position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%);
  display: flex; gap: 8px; z-index: 3; width: 480px; max-width: 90vw;
}
#danmu-input-bar input {
  flex: 1; padding: 10px 14px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.25);
  background: rgba(0,0,0,0.5); color: #fff; font-size: 14px; outline: none;
}
#danmuSend {
  padding: 10px 18px; border-radius: 8px; border: none; cursor: pointer;
  background: linear-gradient(135deg, #00d4aa, #2563eb); color: #fff; font-weight: 600;
}
```

- [ ] **Step 3: JS — 弹幕发送与显示**

```javascript
  // ---- 直播测试台：弹幕输入 / 显示 ----
  var danmuInput = document.getElementById("danmuInput");
  var danmuSend = document.getElementById("danmuSend");
  var danmuList = document.getElementById("danmuList");
  var MAX_DANMU = 50;

  function postDanmaku(text) {
    fetch("/api/danmaku", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: text, user_name: "测试观众" })
    }).then(function (r) { return r.json(); }).catch(function () {
      pushDanmu("测试观众", "（网络错误，弹幕未发送）", "filtered");
    });
  }
  function pushDanmu(user, text, cls) {
    var el = document.createElement("div");
    el.className = "dm-item" + (cls ? " " + cls : "");
    el.innerHTML = '<span class="dm-user">' + esc(user) + '</span>' + esc(text);
    danmuList.appendChild(el);
    while (danmuList.children.length > MAX_DANMU) danmuList.removeChild(danmuList.firstChild);
  }
  danmuSend.addEventListener("click", function () {
    var t = danmuInput.value.trim();
    if (!t) return;
    pushDanmu("测试观众", t, "");
    postDanmaku(t);
    danmuInput.value = "";
  });
  danmuInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); danmuSend.click(); }
  });
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
```

- [ ] **Step 4: JS — 事件订阅（弹幕显示 + AI 字幕 + TTS 播放）**

在现有 store.subscribe 回调中追加：

```javascript
    if (event.type === "danmaku:received") {
      pushDanmu(event.user_name || "观众", event.content || "", "");
    } else if (event.type === "audience:filtered") {
      pushDanmu("拦截", event.content || "", "filtered");
    } else if (event.type === "frontend:subtitle_update") {
      pushDanmu("AI · " + (event.role || "yuki"), event.text || "", "ai");
    } else if (event.type === "tts:audio_ready") {
      playTtsAudio(event.audio_id, event.role || "yuki");
    }
```

- [ ] **Step 5: JS — TTS 播放器（出声修复）**

```javascript
  // ---- TTS 播放：/ws/tts_audio 拉音频字节 → HTML5 Audio ----
  var ttsSock = null;
  var ttsAudioEls = {};   // role -> Audio
  var playedAudioIds = {};
  function ensureTtsSock() {
    if (ttsSock && (ttsSock.readyState === 0 || ttsSock.readyState === 1)) return ttsSock;
    ttsSock = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/tts_audio");
    ttsSock.onmessage = function (ev) {
      // 二进制音频字节
      if (ev.data instanceof Blob && ttsSock._pendingAudioId) {
        var url = URL.createObjectURL(ev.data);
        var role = ttsSock._pendingRole || "yuki";
        var audio = ttsAudioEls[role] || (ttsAudioEls[role] = new Audio());
        audio.src = url;
        audio.play().catch(function () {});
        ttsSock._pendingAudioId = null;
      }
    };
    return ttsSock;
  }
  function playTtsAudio(audioId, role) {
    if (!audioId || playedAudioIds[audioId]) return;
    playedAudioIds[audioId] = true;
    var ws = ensureTtsSock();
    if (ws.readyState === 1) {
      ws._pendingAudioId = audioId;
      ws._pendingRole = role;
      ws.send(JSON.stringify({ audio_id: audioId }));
    }
  }
```

- [ ] **Step 6: node 语法检查**

Run: `node --check`（提取 script 块）
Expected: 语法通过

- [ ] **Step 7: 提交**

```bash
git add frontend/live2d_stream/index.html
git commit -m "feat(live2d_stream): 弹幕输入/显示 + TTS 播放 + 背景装饰（直播测试台）"
```

---

### Task 4: 全量回归 + 端到端验收

**Files:**
- 无新增

- [ ] **Step 1: 后端全量回归**

Run: `python -m pytest -q`
Expected: 全部通过

- [ ] **Step 2: 启动系统手动验收**

Run: `python src/app.py`，浏览器访问 `http://localhost:5000/live2d/`：
- 直播间背景装饰层显示（banner/底栏/弹幕面板）
- 输入弹幕"你好" → 弹幕显示框出现"测试观众: 你好"
- AI 字幕显示（AI · yuki: 回复内容）
- TTS 出声（浏览器播放音频）
- Live2D 口型随 TTS 动
- 等待冷场（或手动触发）→ 主动说话

- [ ] **Step 3: 提交最终验证（如有改动）**

```bash
git add -A
git commit -m "chore(verify): 直播测试台端到端验收"  # 若仅验证无改动则跳过
```

---

# 验收清单（最终）

1. `POST /api/danmaku` 返回 ok + command_id，发布 `danmaku:received`。
2. 弹幕输入 → AI 回复字幕显示。
3. TTS 出声（Audio 播放）。
4. Live2D 口型随 TTS 动（后端 lip_sync_start 真实发出）。
5. 冷场触发主动说话（字幕 + TTS + 口型）。
6. 直播间背景装饰层显示。
7. 全部 pytest 通过。
