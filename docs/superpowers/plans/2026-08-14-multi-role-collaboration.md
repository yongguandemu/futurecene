# 多角色联合（Multi-Role Collaboration）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Yuki（Hiyori 形象）与 Lilith（小恶魔形象）同时在场、同屏渲染、独立回应弹幕/指令，并支持仲裁器驱动的完整双人对话（接话/吐槽/引用/补充 + 冷场自发闲聊）。

**Architecture:** 联动逻辑全部集中于 `src/orchestrators/collaboration/`（与调度官平级）：`coordinator` 拦截 `danmaku:received` → `arbitrator` 按可插拔规则链（@指定→意图→相关性→冷却→随机，零 LLM）决定发言人 → 复用 `DanmakuPipeline.execute_with(role, system_prompt, turn_context)` 执行既有链路 → `triggers` 监听 `speech:completed` 产出接话提案。角色在场模型（`SessionContext.present_roles`）+ 事件全程携带 `role`（TTS/Live2D/字幕），前端双模型同台渲染并按 role 路由，Yuki（Hiyori）呼吸动作经 `restrict_breath` 状态机压制。

**Tech Stack:** Python 3.10 + Flask + EventBus（后端）；原生 JS + store.js + pixi-live2d-display（前端，无构建步骤）；TDD（pytest）。

**Spec:** `docs/superpowers/specs/2026-08-14-multi-role-collaboration-design.md`

---

## 文件结构

```
新增：
src/commander/character_profile.py        角色配置加载器（profiles → persona/keywords/voice）
src/orchestrators/collaboration/__init__.py
src/orchestrators/collaboration/arbitrator.py   发言权仲裁器（互斥 + 队列 + 规则链）
src/orchestrators/collaboration/context_manager.py  多角色上下文（记忆分桶 + 全局流 + 感知彼此）
src/orchestrators/collaboration/turn_tracker.py     话轮追踪（谁在说/待发队列/冷却）
src/orchestrators/collaboration/rules.py            可插拔规则集（5 内置规则）
src/orchestrators/collaboration/triggers.py         联动触发（接话/吐槽/引用/补充）
src/orchestrators/collaboration/coordinator.py      顶层协调器（组装 + 事件接线）
assets/live2d/Hiyori/                               迁移自旧项目（Yuki 形象）

修改：
src/shared/events.py                       +4 事件 + ALL_EVENTS；TTS/LIVE2D 事件 role 契约
src/commander/session_context.py           在场模型（present_roles/lead_role/add_role/remove_role）
src/orchestrators/memory_orchestrator/memory_orchestrator.py  记忆分桶 character_id
src/orchestrators/tts_orchestrator/tts_orchestrator.py        TTS_AUDIO_READY 补 role
src/orchestrators/live2d_orchestrator/live2d_orchestrator.py  多模型状态机 + 事件带 role
src/web/state_provider.py                  快照 + characters 段
src/commander/state_publisher.py           触发事件列表扩展
src/commander/danmaku_pipeline.py          execute_with 参数化入口 + speech:completed
src/web/app_factory.py                     POST /api/collab/config
src/app.py                                 装配 collaboration（enabled 开关）
config/config.yaml                         collaboration 域 + roles 映射
frontend/assets/store.js                   characters 状态
frontend/live2d_stream/index.html          双模型渲染 + 呼吸限制 + 映射
frontend/assistant/index.html              @ 定向 + 角色在场 UI
```

---

# P0 · 基础层（数据 / 事件 / 配置）

### Task 1: 事件契约扩展（新事件 + role 字段）

**Files:**
- Modify: `src/shared/events.py`
- Modify: `tests/test_events_schema.py`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_events_schema.py`）

```python
def test_collaboration_events_registered():
    from src.shared import events
    for name in ("character:presence_changed", "speech:arbitrated",
                 "speech:completed", "collab:utterance_requested"):
        const = getattr(events, name.upper().replace(":", "_").replace("-", "_"))
        assert const in events.ALL_EVENTS
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_events_schema.py::test_collaboration_events_registered -v`
Expected: FAIL（AttributeError / AssertionError）

- [ ] **Step 3: 实现**（`src/shared/events.py`，在 会话域 后新增"多角色协作域"）

```python
# ========== 多角色协作域 ==========
CHARACTER_PRESENCE_CHANGED = "character:presence_changed"  # 角色在场变更（触发 state:changed）
SPEECH_ARBITRATED = "speech:arbitrated"                    # 仲裁结果（role/rule_hit/request_id）
SPEECH_COMPLETED = "speech:completed"                      # 发言完成（role/text/audio_id，触发接话决策）
COLLAB_UTTERANCE_REQUESTED = "collab:utterance_requested"  # 联动发言请求（role/kind/reason/ref_text）
```

并将 4 个常量加入 `ALL_EVENTS` frozenset；同时在 `TTS_AUDIO_READY` 注释后追加契约说明：`payload 含 role`。

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_events_schema.py -v`
Expected: PASS

Run: `python -m pytest tests/test_events_schema.py tests/test_event_bus.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/shared/events.py tests/test_events_schema.py
git commit -m "feat(events): 新增多角色协作域事件与 ALL_EVENTS 收录"
```

---

### Task 2: SessionContext 在场模型

**Files:**
- Modify: `src/commander/session_context.py`
- Modify: `tests/test_state_provider.py`（或新建 `tests/test_session_context.py`）

- [ ] **Step 1: 写失败测试**（新建 `tests/test_session_context.py`）

```python
"""session_context 在场模型测试。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.shared.event_bus import EventBus
from src.commander.session_context import SessionContext


def test_presence_model():
    bus = EventBus()
    session = SessionContext(session_id="default")
    session.bind_event_bus(bus)
    events = []
    bus.subscribe("character:presence_changed", lambda **kw: events.append(kw))

    assert session.present_roles == {"yuki"}
    session.add_role("lilith")
    assert session.present_roles == {"yuki", "lilith"}
    assert len(events) == 1 and events[0]["role"] == "lilith"

    session.set_lead("lilith")
    assert session.lead_role == "lilith"

    session.remove_role("lilith")
    assert session.present_roles == {"yuki"}


def test_switch_role_compat():
    bus = EventBus()
    session = SessionContext(session_id="default")
    session.bind_event_bus(bus)
    session.add_role("lilith")
    assert session.switch_role("lilith") is True   # 兼容：设焦点角色
    assert session.role == "lilith"
    assert session.snapshot()["role"] == "lilith"
    assert set(session.snapshot()["present_roles"]) == {"yuki", "lilith"}
    assert session.switch_role("nobody") is False
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_session_context.py -v`
Expected: FAIL（AttributeError: present_roles）

- [ ] **Step 3: 实现**（`src/commander/session_context.py`）

```python
from src.shared.events import CHARACTER_PRESENCE_CHANGED, SESSION_SWITCHED


@dataclass
class SessionContext:
    """会话上下文（指挥官持有，唯一状态源）。"""
    session_id: str
    role: str = "yuki"          # 焦点角色（兼容现有逻辑）
    scene: str = "chat"
    live_mode: str = "offline"
    started_at: float = field(default_factory=time.time)
    lead_role: str = "yuki"     # 主角色：系统意图归属（不独占发言权）
    present_roles: set = field(default_factory=lambda: {"yuki"})  # 在场角色集合

    def __post_init__(self):
        self._event_bus = None

    def bind_event_bus(self, event_bus) -> None:
        self._event_bus = event_bus

    def add_role(self, role: str) -> bool:
        if role not in VALID_ROLES or role in self.present_roles:
            return False
        self.present_roles.add(role)
        if self._event_bus is not None:
            self._event_bus.publish(CHARACTER_PRESENCE_CHANGED, role=role,
                                    present=True, session_id=self.session_id)
        return True

    def remove_role(self, role: str) -> bool:
        if role not in self.present_roles:
            return False
        self.present_roles.discard(role)
        if role == self.role:
            self.role = "yuki" if "yuki" in self.present_roles else min(self.present_roles)
        if role == self.lead_role:
            self.lead_role = self.role
        if self._event_bus is not None:
            self._event_bus.publish(CHARACTER_PRESENCE_CHANGED, role=role,
                                    present=False, session_id=self.session_id)
        return True

    def set_lead(self, role: str) -> bool:
        if role not in self.present_roles:
            return False
        self.lead_role = role
        return True

    def switch_role(self, role: str) -> bool:
        """切换焦点角色；兼容现状。多角色模式不改变在场集合。"""
        if role not in VALID_ROLES:
            return False
        self.role = role
        if role not in self.present_roles:
            self.present_roles.add(role)   # 单角色模式：切换即在场
        if self._event_bus is not None:
            self._event_bus.publish(SESSION_SWITCHED, role=role,
                                    session_id=self.session_id)
        return True

    def switch_scene(self, scene: str) -> bool:
        if scene not in VALID_SCENES:
            return False
        self.scene = scene
        return True

    def snapshot(self) -> Dict[str, Any]:
        return {"session_id": self.session_id, "role": self.role,
                "scene": self.scene, "live_mode": self.live_mode,
                "started_at": self.started_at, "lead_role": self.lead_role,
                "present_roles": sorted(self.present_roles)}
```

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_session_context.py -v`
Expected: PASS

Run: `python -m pytest -q`
Expected: 全部通过（既有 265 + 新增）

- [ ] **Step 5: 提交**

```bash
git add src/commander/session_context.py tests/test_session_context.py
git commit -m "feat(session): 角色在场模型 present_roles/lead_role（向后兼容）"
```

---

### Task 3: CharacterProfile 加载器

**Files:**
- Create: `src/commander/character_profile.py`
- Create: `tests/test_character_profile.py`

- [ ] **Step 1: 写失败测试**（新建 `tests/test_character_profile.py`）

```python
"""character_profile 加载器测试（临时 profile 目录）。"""
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.commander.character_profile import CharacterProfileLoader


def _make_profiles(tmp: Path):
    r = tmp / "profiles" / "yuki"
    r.mkdir(parents=True)
    (r / "character.yaml").write_text(
        "display_name: Yuki\n"
        "character:\n"
        "  personality: [温柔, 害羞]\n"
        "  catchphrase: 嗯~\n"
        "  speaking_style: 温柔\n"
        "keywords:\n"
        "  topics: [故事, 月亮]\n"
        "  patterns: [\"讲个故事\", \"regex:月亮.*邮差\"]\n",
        encoding="utf-8")
    (r / "system_prompt.txt").write_text("你是Yuki，温柔害羞。", encoding="utf-8")
    (r / "tts_config.yaml").write_text("tts:\n  wusound:\n    voice_id: v-yuki\n",
                                       encoding="utf-8")


def test_load_profile():
    with tempfile.TemporaryDirectory() as d:
        _make_profiles(Path(d))
        loader = CharacterProfileLoader(profiles_dir=Path(d) / "profiles")
        p = loader.load("yuki")
        assert p.system_prompt == "你是Yuki，温柔害羞。"
        assert "故事" in p.keywords["topics"]
        assert "regex:月亮.*邮差" in p.keywords["patterns"]
        assert p.voice_id == "v-yuki"


def test_keywords_fallback_without_keywords_field():
    with tempfile.TemporaryDirectory() as d:
        r = Path(d) / "profiles" / "lilith"
        r.mkdir(parents=True)
        (r / "character.yaml").write_text(
            "display_name: Lilith\n"
            "character:\n"
            "  personality: [毒舌, 冷静]\n"
            "  catchphrase: 呵\n"
            "  speaking_style: 犀利\n",
            encoding="utf-8")
        (r / "system_prompt.txt").write_text("你是Lilith。", encoding="utf-8")
        loader = CharacterProfileLoader(profiles_dir=Path(d) / "profiles")
        kw = loader.keywords_for("lilith")
        # 兜底推导：personality + catchphrase + speaking_style 分词
        assert any("毒舌" in k for k in kw["topics"]) or "毒舌" in kw["personality"]


def test_load_missing_role_returns_none():
    with tempfile.TemporaryDirectory() as d:
        loader = CharacterProfileLoader(profiles_dir=Path(d) / "profiles")
        assert loader.load("ghost") is None


def test_all_roles_lists_dirs():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "profiles" / "yuki").mkdir(parents=True)
        (Path(d) / "profiles" / "lilith").mkdir(parents=True)
        loader = CharacterProfileLoader(profiles_dir=Path(d) / "profiles")
        assert set(loader.all_roles()) == {"yuki", "lilith"}
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_character_profile.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现**（新建 `src/commander/character_profile.py`）

```python
"""character_profile.py — 角色配置加载器（config/profiles/{role}/）

读取 character.yaml（v2.1 含 keywords）/ system_prompt.txt / tts_config.yaml / catchphrases.json。
keywords 缺失时从 personality/catchphrase/speaking_style 兜底推导（零配置可降级）。
"""
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

PROFILES_DIR = Path(__file__).resolve().parents[2] / "config" / "profiles"


@dataclass
class CharacterProfile:
    role: str
    display_name: str = ""
    system_prompt: str = ""
    keywords: Dict[str, List[str]] = field(default_factory=dict)
    voice_id: str = ""
    catchphrases: List[Dict] = field(default_factory=list)


class CharacterProfileLoader:
    """按角色加载配置；缺失角色返回 None。"""

    def __init__(self, profiles_dir: Optional[Path] = None, yaml=None, json=None):
        self._dir = Path(profiles_dir) if profiles_dir else PROFILES_DIR
        self._yaml = yaml
        self._json = json
        self._cache: Dict[str, Optional[CharacterProfile]] = {}

    def _imports(self):
        if self._yaml is None or self._json is None:
            import json
            import yaml
            self._yaml = yaml
            self._json = json
        return self._yaml, self._json

    def all_roles(self) -> List[str]:
        if not self._dir.exists():
            return []
        return sorted(p.name for p in self._dir.iterdir() if p.is_dir())

    def load(self, role: str) -> Optional[CharacterProfile]:
        if role in self._cache:
            return self._cache[role]
        role_dir = self._dir / role
        if not role_dir.is_dir():
            self._cache[role] = None
            return None
        yaml, json_mod = self._imports()
        profile = CharacterProfile(role=role)
        char_yaml = role_dir / "character.yaml"
        if char_yaml.exists():
            try:
                data = yaml.safe_load(char_yaml.read_text(encoding="utf-8")) or {}
                profile.display_name = data.get("display_name", role)
                char = data.get("character", {}) or {}
                profile.keywords = self._derive_keywords(data, char)
            except Exception as e:
                logger.warning("[CharacterProfile] %s character.yaml 解析失败: %s", role, e)
        sp = role_dir / "system_prompt.txt"
        if sp.exists():
            profile.system_prompt = sp.read_text(encoding="utf-8").strip()
        tts_yaml = role_dir / "tts_config.yaml"
        if tts_yaml.exists():
            try:
                tts = yaml.safe_load(tts_yaml.read_text(encoding="utf-8")) or {}
                wu = tts.get("tts", {}).get("wusound", {}) or {}
                profile.voice_id = wu.get("voice_id", "")
            except Exception:
                pass
        cp = role_dir / "catchphrases.json"
        if cp.exists():
            try:
                profile.catchphrases = (json_mod.loads(cp.read_text(encoding="utf-8"))
                                        .get("phrases", []))
            except Exception:
                pass
        self._cache[role] = profile
        return profile

    def keywords_for(self, role: str) -> Dict[str, List[str]]:
        p = self.load(role)
        return p.keywords if p else {}

    @staticmethod
    def _derive_keywords(data: dict, char: dict) -> Dict[str, List[str]]:
        """优先用 character.yaml 的 keywords 字段；缺失则兜底推导。"""
        kw = data.get("keywords") or {}
        if kw:
            return {"personality": list(kw.get("personality", []) or []),
                    "topics": list(kw.get("topics", []) or []),
                    "patterns": list(kw.get("patterns", []) or [])}
        derived = []
        for label in (char.get("personality", []) or []):
            derived.append(str(label))
        if char.get("catchphrase"):
            derived.append(str(char["catchphrase"]).strip("~～,，。"))
        for token in re.split(r"[，,。;；\s]+", str(char.get("speaking_style", ""))):
            token = token.strip()
            if token and len(token) >= 2:
                derived.append(token)
        return {"personality": [str(x) for x in (char.get("personality", []) or [])],
                "topics": derived, "patterns": []}
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_character_profile.py -v`
Expected: PASS（若环境缺 PyYAML 先 `pip install pyyaml`，项目 requirements 已含）

- [ ] **Step 5: 提交**

```bash
git add src/commander/character_profile.py tests/test_character_profile.py
git commit -m "feat(profile): 角色配置加载器（keywords 定义 + 兜底推导）"
```

---

### Task 4: 记忆分桶（character_id）

**Files:**
- Modify: `src/orchestrators/memory_orchestrator/memory_orchestrator.py`
- Modify: `tests/test_memory_orchestrator.py`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_memory_orchestrator.py`）

```python
def test_memory_bucket_by_character():
    import asyncio
    from src.shared.event_bus import EventBus
    from src.orchestrators.memory_orchestrator.memory_orchestrator import MemoryOrchestrator
    orch = MemoryOrchestrator(EventBus())
    orch.start()
    asyncio.run(orch.handle({"capability": "memory:store",
                             "payload": {"content": "Yuki 的话题", "role": "assistant",
                                         "session_id": "default", "character_id": "yuki"}}))
    asyncio.run(orch.handle({"capability": "memory:store",
                             "payload": {"content": "Lilith 的话题", "role": "assistant",
                                         "session_id": "default", "character_id": "lilith"}}))
    r1 = asyncio.run(orch.handle({"capability": "memory:get_history",
                                  "payload": {"session_id": "default",
                                              "character_id": "yuki", "limit": 20}}))
    texts = [e["content"] for e in r1["data"]["history"]]
    assert "Yuki 的话题" in texts and "Lilith 的话题" not in texts
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_memory_orchestrator.py::test_memory_bucket_by_character -v`
Expected: FAIL（两段内容都在同一桶，断言失败）

- [ ] **Step 3: 实现**（`src/orchestrators/memory_orchestrator/memory_orchestrator.py`）

在 `_store`/`_retrieve`/`_get_history`/`_consolidate` 内，将 session 键替换为分桶键：

```python
    @staticmethod
    def _bucket(session_id: str, character_id: str = "") -> str:
        """记忆分桶：character_id 存在时按角色隔离。"""
        return f"{session_id}:{character_id}" if character_id else session_id

    def _store(self, payload):
        content = payload.get("content", "")
        if not content:
            return {"ok": False, "data": {}, "error": "content 必填"}
        session_id = payload.get("session_id", "default")
        bucket = self._bucket(session_id, payload.get("character_id", ""))
        memory_id = self._short.append(bucket, payload.get("role", "user"), content)
        self._event_bus.publish(MEMORY_STORED, memory_id=memory_id, session_id=session_id)
        return {"ok": True, "data": {"memory_id": memory_id}, "error": None}

    def _retrieve(self, payload):
        # 仅把 session_id 读取处改为 bucket（其余逻辑不变）
        session_id = payload.get("session_id", "")
        bucket = self._bucket(session_id, payload.get("character_id", ""))
        short_entries = self._short.get_history(bucket) if session_id else self._short.all_entries()
        # ... 其余与现状一致 ...

    def _get_history(self, payload):
        session_id = payload.get("session_id", "default")
        bucket = self._bucket(session_id, payload.get("character_id", ""))
        limit = int(payload.get("limit", 20))
        history = self._short.get_history(bucket, limit)
        return {"ok": True, "data": {"history": history}, "error": None}
```

> 注：`_retrieve` 只改 `short_entries` 来源行；`_consolidate` 按 `character_id` 分桶固化，逻辑同理（`bucket` 替换 `session_id` 读取处）。

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_memory_orchestrator.py -v`
Expected: PASS

Run: `python -m pytest tests/test_danmaku_pipeline.py tests/test_memory_orchestrator.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/orchestrators/memory_orchestrator/memory_orchestrator.py tests/test_memory_orchestrator.py
git commit -m "feat(memory): 记忆按 character_id 分桶"
```

---

### Task 5: 快照 characters 段 + 发布触发扩展

**Files:**
- Modify: `src/web/state_provider.py`
- Modify: `src/commander/state_publisher.py`
- Modify: `tests/test_state_provider.py`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_state_provider.py`）

```python
def test_snapshot_has_characters():
    bus = EventBus()
    provider = StateProvider(event_bus=bus, session=FakeSession(),
                             switch_manager=FakeSwitchManager(),
                             registry=FakeRegistry(),
                             degradation_manager=FakeDegradation(),
                             metrics_provider=FakeMetrics(),
                             characters_provider=lambda: {"yuki": {"present": True}})
    snap = provider.snapshot()
    assert "characters" in snap
    assert snap["characters"]["yuki"]["present"] is True


def test_state_publisher_triggers_on_presence():
    bus = EventBus()
    bus.reset()
    from src.commander.state_publisher import StatePublisher
    provider = StateProvider(event_bus=bus, session=FakeSession(),
                             switch_manager=FakeSwitchManager(),
                             registry=FakeRegistry(),
                             degradation_manager=FakeDegradation(),
                             metrics_provider=FakeMetrics())
    publisher = StatePublisher(bus, provider)
    publisher.start()
    got = []
    bus.subscribe("state:changed", lambda **kw: got.append(kw))
    bus.publish("character:presence_changed", role="lilith", present=True)
    assert len(got) == 1
    publisher.stop()
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_state_provider.py::test_snapshot_has_characters -v`
Expected: FAIL（`set(snap.keys()) == {...}` 冲突 → 需同步修改既有 `test_snapshot_structure` 断言）

- [ ] **Step 3: 实现**（`src/web/state_provider.py`）

```python
    def __init__(self, event_bus, session=None, switch_manager=None,
                 registry=None, degradation_manager=None,
                 metrics_provider=None, characters_provider=None):
        # ... 既有注入 ...
        self._characters = characters_provider  # Callable[[], Dict[str, dict]] | None

    def snapshot(self) -> Dict[str, Any]:
        version = self._event_bus.current_seq() if self._event_bus else 0
        metrics = self._metrics() if self._metrics else {}
        session_snap = self._session.snapshot() if self._session else {}
        characters = self._characters() if self._characters else {}
        if not characters and session_snap:
            characters = {r: {"present": True}
                          for r in session_snap.get("present_roles", [])}
        return {
            "version": version,
            "session": session_snap,
            "switches": self._switch_manager.snapshot() if self._switch_manager else {},
            "orchestrators": [o.name for o in self._registry.all()]
            if self._registry else [],
            "degradation": self._degradation.snapshot() if self._degradation else {},
            "cost": metrics.get("cost", {}),
            "watchdog": metrics.get("watchdog", {}),
            "characters": characters,
        }
```

同步修改 `tests/test_state_provider.py` 既有 `test_snapshot_structure` 期望集合，加入 `"characters"`。

`src/commander/state_publisher.py` 触发事件列表：

```python
_TRIGGER_EVENTS = [
    SWITCH_CHANGED,
    SESSION_SWITCHED,
    SESSION_STATE_CHANGED,
    CHARACTER_PRESENCE_CHANGED,
    SPEECH_ARBITRATED,
    SPEECH_COMPLETED,
    "degradation:*",
    "watchdog:*",
    COST_CIRCUIT_OPEN,
]
```

并更新 import：`from src.shared.events import (..., CHARACTER_PRESENCE_CHANGED, SPEECH_ARBITRATED, SPEECH_COMPLETED)`。

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_state_provider.py -v`
Expected: PASS

Run: `python -m pytest -q`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add src/web/state_provider.py src/commander/state_publisher.py tests/test_state_provider.py
git commit -m "feat(state): 快照 characters 段 + presence/speech 触发 state:changed"
```

---

# P1 · 双模型渲染

### Task 6: Hiyori 资产迁移 + config 映射

**Files:**
- Create: `assets/live2d/Hiyori/**`（迁移）
- Modify: `config/config.yaml`（collaboration 域 + roles）

- [ ] **Step 1: 迁移 Hiyori 模型**

Run（PowerShell，从旧项目复制）:

```powershell
Copy-Item -Recurse 'D:\future scene\LumiProject\assets\live2d\Hiyori' 'd:\future scene V2\assets\live2d\Hiyori'
```

- [ ] **Step 2: 验证加载路径**

Run: `python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:5000/assets/live2d/Hiyori/Hiyori.model3.json', timeout=5).status)"`
（若后端未运行先 `python src/app.py`；Expected: 200）

- [ ] **Step 3: 新增配置**（`config/config.yaml` 末尾追加）

```yaml
# ---------- 多角色协作（P2 装配，P1 先备好角色映射） ----------
collaboration:
  enabled: false
  rules_order: [mention, intent, relevance, cooldown, random]
  lead_role: yuki
  trigger_probability: 0.3
  trigger_global_cooldown: 20.0
  awareness:
    enabled: true
    max_partner_lines: 2

roles:
  - name: yuki
    live2d_model: Hiyori
    restrict_breath: true
    idle_silent_motion: Hiyori_m03
    position: {x: 0.27, y: 1.0}
    scale: 0.42
    expression_map: {}
    motion_map:
      wave: Hiyori_m01
      nod: Hiyori_m02
      shake: Hiyori_m05
      idle: Hiyori_m01
  - name: lilith
    live2d_model: 小恶魔
    restrict_breath: false
    position: {x: 0.73, y: 1.0}
    scale: 0.42
    expression_map: {开心: 唱歌, 难过: 流泪, 惊讶: 头发, 害羞: 嘟嘴, 生气: 脸黑, 平静: 头发}
    motion_map: {wave: wave, nod: nod, shake: shake, idle: idle}
```

- [ ] **Step 4: 配置解析冒烟**

Run: `python -c "from src.shared.config_loader import ConfigLoader; c=ConfigLoader(); print(c.get('collaboration.enabled'), c.get('roles.0.name'))"`
Expected: `False yuki`

- [ ] **Step 5: 提交**

```bash
git add assets/live2d/Hiyori config/config.yaml
git commit -m "feat(assets): 迁移 Hiyori（Yuki 形象）+ collaboration/roles 配置"
```

---

### Task 7: Live2D 调度官多模型状态机

**Files:**
- Modify: `src/orchestrators/live2d_orchestrator/live2d_orchestrator.py`
- Modify: `tests/test_live2d_orchestrator.py`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_live2d_orchestrator.py`）

```python
def test_multi_model_events_carry_role():
    import asyncio
    from src.shared.event_bus import EventBus
    from src.orchestrators.live2d_orchestrator.live2d_orchestrator import Live2DOrchestrator
    bus = EventBus()
    orch = Live2DOrchestrator(bus)
    orch.start()
    events = []
    bus.subscribe("live2d:loaded", lambda **kw: events.append(kw))
    asyncio.run(orch.handle({"capability": "live2d:load",
                             "payload": {"model_name": "Hiyori", "role": "yuki"}}))
    assert events[-1]["role"] == "yuki" and events[-1]["model"] == "Hiyori"


def test_audio_ready_routes_lip_sync_by_role():
    import asyncio
    from src.shared.event_bus import EventBus
    from src.orchestrators.live2d_orchestrator.live2d_orchestrator import Live2DOrchestrator
    bus = EventBus()
    orch = Live2DOrchestrator(bus)
    orch.start()
    asyncio.run(orch.handle({"capability": "live2d:load",
                             "payload": {"model_name": "Hiyori", "role": "yuki"}}))
    got = []
    bus.subscribe("live2d:lip_sync_start", lambda **kw: got.append(kw))
    bus.publish("tts:audio_ready", audio_id="a1", duration_ms=500, role="yuki")
    assert got and got[0]["role"] == "yuki" and got[0]["audio_id"] == "a1"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_live2d_orchestrator.py::test_multi_model_events_carry_role -v`
Expected: FAIL（事件无 role）

- [ ] **Step 3: 实现**（`src/orchestrators/live2d_orchestrator/live2d_orchestrator.py`）

```python
DEFAULT_MODEL = "小恶魔"
VALID_EXPRESSIONS = {"开心", "难过", "惊讶", "害羞", "生气", "平静"}
VALID_MOTIONS = {"wave", "nod", "shake", "idle"}


class Live2DOrchestrator:
    name = "live2d"

    def __init__(self, event_bus):
        self._event_bus = event_bus
        self._models: Dict[str, Dict[str, Any]] = {}   # role -> ModelState
        self._lip_threads: Dict[str, threading.Thread] = {}
        self._started = False
        registry.bind(self.handle)

    def capabilities(self):
        return registry.capabilities()

    def start(self):
        if self._started:
            return
        self._event_bus.subscribe(TTS_AUDIO_READY, self._on_audio_ready)
        self._started = True

    def stop(self):
        self._event_bus.unsubscribe(TTS_AUDIO_READY, self._on_audio_ready)
        self._started = False

    def health(self):
        return {"status": "ok" if self._started else "down",
                "detail": f"models={list(self._models.keys()) or '未加载'}"}

    def snapshot(self):
        first = next(iter(self._models.values()), {})
        return {"model": first.get("model"), "expression": first.get("expression"),
                "motion": first.get("motion"), "lip_sync": dict(first.get("lip_sync", {})),
                "models": {r: dict(m) for r, m in self._models.items()}}

    async def handle(self, command):
        capability = command.get("capability", "")
        payload = command.get("payload", {})
        if capability == "live2d:load":
            return self._load(payload)
        if capability == "live2d:expression":
            return self._expression_change(payload)
        if capability == "live2d:motion":
            return self._motion_trigger(payload)
        if capability == "live2d:lip_sync":
            return self._lip_sync(payload)
        return {"ok": False, "data": {}, "error": f"unknown capability: {capability}"}

    # ---------- 内部 ----------

    def _state(self, role: str) -> Dict[str, Any]:
        """取/建角色模型状态。"""
        if role not in self._models:
            self._models[role] = {"model": None, "expression": "平静",
                                  "motion": "idle", "lip_sync": {}}
        return self._models[role]

    def _load(self, payload):
        role = payload.get("role", "yuki")
        model_name = payload.get("model_name", DEFAULT_MODEL)
        st = self._state(role)
        st["model"] = model_name
        self._event_bus.publish(LIVE2D_LOADED, model=model_name, role=role)
        self._push_status()
        return {"ok": True, "data": {"loaded": True, "model": model_name, "role": role},
                "error": None}

    def _expression_change(self, payload):
        role = payload.get("role", "yuki")
        st = self._state(role)
        if st["model"] is None:
            return {"ok": False, "data": {}, "error": "模型未加载，请先 live2d:load"}
        expression = payload.get("expression", "平静")
        if expression not in VALID_EXPRESSIONS:
            return {"ok": False, "data": {}, "error": f"未知表情: {expression}"}
        st["expression"] = expression
        self._event_bus.publish(LIVE2D_EXPRESSION_CHANGED, expression=expression, role=role)
        self._push_status()
        return {"ok": True, "data": {"applied": True}, "error": None}

    def _motion_trigger(self, payload):
        role = payload.get("role", "yuki")
        st = self._state(role)
        if st["model"] is None:
            return {"ok": False, "data": {}, "error": "模型未加载，请先 live2d:load"}
        motion = payload.get("motion", "idle")
        if motion not in VALID_MOTIONS:
            return {"ok": False, "data": {}, "error": f"未知动作: {motion}"}
        st["motion"] = motion
        self._event_bus.publish(LIVE2D_MOTION_TRIGGERED, motion=motion, role=role)
        self._push_status()
        return {"ok": True, "data": {"triggered": True}, "error": None}

    def _lip_sync(self, payload):
        return self._start_lip_sync(payload.get("role", "yuki"),
                                    payload.get("audio_id", ""),
                                    int(payload.get("duration_ms", 1500)))

    def _start_lip_sync(self, role, audio_id, duration_ms):
        st = self._state(role)
        if st["model"] is None:
            return {"ok": False, "data": {}, "error": "模型未加载，请先 live2d:load"}
        st["lip_sync"] = {"audio_id": audio_id, "started_at": time.time(),
                          "duration_ms": duration_ms}
        self._event_bus.publish(LIVE2D_LIP_SYNC_START, audio_id=audio_id,
                                duration_ms=duration_ms, role=role)
        self._push_status()

        def _end(r=role, aid=audio_id):
            time.sleep(max(duration_ms, 50) / 1000.0)
            if self._models.get(r, {}).get("lip_sync", {}).get("audio_id") == aid:
                self._models[r]["lip_sync"] = {}
                self._event_bus.publish(LIVE2D_LIP_SYNC_END, audio_id=aid, role=r)
                self._push_status()

        self._lip_threads[role] = threading.Thread(target=_end, daemon=True,
                                                   name=f"Live2D-lip-{role}")
        self._lip_threads[role].start()
        return {"ok": True, "data": {"started": True}, "error": None}

    def _on_audio_ready(self, event, audio_id, duration_ms=1500, role="yuki", **kwargs):
        """表达领域协作：按 role 路由口型。"""
        self._start_lip_sync(role, audio_id, duration_ms)

    def _push_status(self):
        self._event_bus.publish(FRONTEND_STATUS_UPDATE, domain="live2d",
                                data=self.snapshot())
```

> 兼容说明：`snapshot()` 保留旧字段（model/expression/motion/lip_sync 取第一个角色），新增 `models` 按角色返回，既有 dashboard 不破坏。

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_live2d_orchestrator.py -v`
Expected: PASS（既有测试若断言 snapshot 结构，兼容字段保留）

Run: `python -m pytest tests/test_p1_pipeline_integration.py tests/test_live2d_orchestrator.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/orchestrators/live2d_orchestrator/live2d_orchestrator.py tests/test_live2d_orchestrator.py
git commit -m "feat(live2d): 多模型状态机 + 事件携带 role + 口型按角色路由"
```

---

### Task 8: 双模型同台渲染 + 呼吸限制状态机

**Files:**
- Modify: `frontend/live2d_stream/index.html`

- [ ] **Step 1: 浏览器冒烟**（改造前基线）

Run: 访问 `http://localhost:5000/live2d/`，确认当前单模型正常。

- [ ] **Step 2: 替换脚本主体**（`frontend/live2d_stream/index.html` 的 `<script>` 块整体替换为下列实现）

```javascript
(function () {
  "use strict";

  // 角色 → 模型配置（与 config.yaml roles 对应；无后端时前端兜底）
  var ROLES = window.__ROLES || [
    { name: "yuki", model: "Hiyori", restrictBreath: true, idleSilent: "Hiyori_m03",
      x: 0.27, scale: 0.42,
      expressionMap: {}, motionMap: { wave: "Hiyori_m01", nod: "Hiyori_m02",
        shake: "Hiyori_m05", idle: "Hiyori_m01" } },
    { name: "lilith", model: "小恶魔", restrictBreath: false, idleSilent: "idle",
      x: 0.73, scale: 0.42,
      expressionMap: { 开心: "唱歌", 难过: "流泪", 惊讶: "头发", 害羞: "嘟嘴", 生气: "脸黑", 平静: "头发" },
      motionMap: { wave: "wave", nod: "nod", shake: "shake", idle: "idle" } }
  ];
  var hint = document.getElementById("hint");
  var app = null;
  var actors = {};   // role -> { model, motion, state: 'silent'|'talking' }

  function showHint(text) { hint.textContent = text; hint.style.display = "block"; }

  function mappedMotion(role, semantic) {
    var cfg = ROLES.find(function (r) { return r.name === role; });
    if (!cfg || !cfg.motionMap) return null;
    return cfg.motionMap[semantic] || cfg.motionMap.idle || null;
  }

  function mappedExpression(role, semantic) {
    var cfg = ROLES.find(function (r) { return r.name === role; });
    if (!cfg || !cfg.expressionMap) return null;
    return cfg.expressionMap[semantic] || null;   // 映射缺失 → null（跳过）
  }

  function playMotion(role, motion) {
    var a = actors[role];
    if (!a || !a.model || !motion) return;
    try { a.model.motion(motion); } catch (e) { /* ignore */ }
  }

  function setState(role, state) {
    var a = actors[role];
    if (!a) return;
    if (a.state === state) return;
    a.state = state;
    var cfg = ROLES.find(function (r) { return r.name === role; });
    if (state === "silent") {
      var idle = cfg && cfg.restrictBreath
        ? (cfg.idleSilent || (cfg.motionMap && cfg.motionMap.idle))
        : (cfg && cfg.motionMap ? cfg.motionMap.idle : null);
      playMotion(role, idle);           // 呼吸压制：只播低动作 idle
    }
  }

  function stopLipSync(role) {
    var a = actors[role];
    if (!a || !a.model) return;
    try {
      a.model.internalModel.motionManager.update(0, 0);
    } catch (e) { /* ignore */ }
  }

  // ---- store 事件处理（按 role 路由） ----
  function handleEvent(type, ev) {
    var role = ev.role || "yuki";
    var a = actors[role];
    if (!a || !a.model) return;
    if (type === "live2d:motion_triggered") {
      playMotion(role, mappedMotion(role, ev.motion));
    } else if (type === "live2d:expression_changed") {
      var expr = mappedExpression(role, ev.expression);
      if (expr) { try { a.model.expression(expr); } catch (e) { /* ignore */ } }
    } else if (type === "live2d:lip_sync_start") {
      // 口型切换：先收尾旧角色（200ms 过渡），再启动新角色口型
      Object.keys(actors).forEach(function (r) {
        if (r !== role) stopLipSync(r);
      });
      setState(role, "talking");
      try {
        var dur = ev.duration_ms || 1500;
        a.model.internalModel.motionManager.update(0, 0);
        setTimeout(function () {
          stopLipSync(role);
          setState(role, "silent");
        }, dur);
      } catch (e) { /* ignore */ }
    }
  }

  function wireStore() {
    if (!window.FSStore || !window.FSStateSync) return;
    var store = window.FSStore.createStore(window.FSStore.makeReducer(), window.FSStore.initialState());
    var sync = new window.FSStateSync(store);
    sync.init();
    store.subscribe("live2d:", function (state, event) { handleEvent(event.type, event); });
    store.subscribe("tts:", function (state, event) {
      if (event.type === "tts:audio_ready") handleEvent("live2d:lip_sync_start", {
        role: event.role || "yuki", audio_id: event.audio_id,
        duration_ms: event.duration_ms });
    });
    store.subscribe("audio:", function (state, event) {
      if (event.type === "audio:segment_ready") handleEvent("live2d:lip_sync_start", {
        role: event.role || "yuki", audio_id: event.audio_id,
        duration_ms: event.duration_ms || 1500 });
    });
    // 静默态兜底：各角色回 idle
    store.subscribe("speech:completed", function (state, event) {
      var role = event.role;
      if (role && actors[role]) {
        setTimeout(function () { stopLipSync(role); setState(role, "silent"); }, 220);
      }
    });
  }

  async function boot() {
    if (!window.PIXI || !window.PIXI.live2d) {
      showHint("PixiJS / Live2D 库加载失败");
      return;
    }
    app = new PIXI.Application({
      view: document.createElement("canvas"), transparent: true, autoStart: true,
      width: 1120, height: 720, antialias: true
    });
    document.getElementById("canvas-holder").appendChild(app.view);
    window.__app = app;

    for (var i = 0; i < ROLES.length; i++) {
      var cfg = ROLES[i];
      var url = "/assets/live2d/" + cfg.model + "/" + cfg.model + ".model3.json";
      try {
        var model = await PIXI.live2d.Live2DModel.from(url);
        model.anchor.set(0.5, 1.0);
        model.scale.set(cfg.scale || 0.42);
        model.x = app.screen.width * (cfg.x !== undefined ? cfg.x : 0.5);
        model.y = app.screen.height;
        app.stage.addChild(model);
        actors[cfg.name] = { model: model, state: "silent" };
        setState(cfg.name, "silent");   // 呼吸限制：显式进入受控 idle
      } catch (e) {
        showHint("模型未就绪：" + url);
      }
    }
    showHint("");
    hint.style.display = "none";
  }

  wireStore();
  boot();
})();
```

- [ ] **Step 3: 语法检查**

Run: `node --check frontend/live2d_stream/index.html` 不可用则跳过；用浏览器打开验证。

- [ ] **Step 4: 浏览器验收**

Run: 访问 `http://localhost:5000/live2d/`
Expected: 双角色同屏（左 Yuki/Hiyori、右 Lilith/小恶魔）；Hiyori 静默呼吸受压制（低动作 idle）；浏览器控制台无报错。

- [ ] **Step 5: 提交**

```bash
git add frontend/live2d_stream/index.html
git commit -m "feat(live2d): 双模型同台渲染 + 呼吸限制状态机 + 角色事件路由"
```

---

# P2 · 联动核心（完整双人对话）

### Task 9: collaboration/rules.py（可插拔规则集）

**Files:**
- Create: `src/orchestrators/collaboration/rules.py`
- Create: `src/orchestrators/collaboration/__init__.py`
- Create: `tests/test_collab_rules.py`

- [ ] **Step 1: 写失败测试**（新建 `tests/test_collab_rules.py`）

```python
"""仲裁规则单测（零 LLM，mock turn_tracker）。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.collaboration.rules import (
    ArbitrationContext, MentionRule, IntentRule, RelevanceRule,
    CooldownRule, RandomRule,
)


class FakeTT:
    def __init__(self, last=None):
        self.last = last or {"yuki": 100.0, "lilith": 50.0}  # yuki 更久未说

    def idle_seconds(self, role):
        return self.last.get(role, 0.0)


class FakeProfiles:
    def keywords_for(self, role):
        return {"yuki": {"topics": ["故事", "月亮"], "patterns": ["讲个故事"]},
                "lilith": {"topics": ["吐槽", "直播"], "patterns": []}}[role]


def _ctx(text, lead="yuki", kind="danmaku"):
    return ArbitrationContext(text=text, user_name="观众", source="danmaku",
                              kind=kind, lead_role=lead,
                              present_roles={"yuki", "lilith"},
                              profiles=FakeProfiles(), turn_tracker=FakeTT())


def test_mention_rule():
    r = MentionRule()
    assert r.evaluate(_ctx("@Lilith 你同意吗")).role == "lilith"
    assert r.evaluate(_ctx("Lilith你怎么看")).role == "lilith"
    assert r.evaluate(_ctx("Yuki酱讲个故事")).role == "yuki"


def test_intent_rule_routes_to_lead():
    r = IntentRule()
    assert r.evaluate(_ctx("下播", lead="yuki")).role == "yuki"
    assert r.evaluate(_ctx("!状态", lead="lilith")).role == "lilith"


def test_relevance_rule():
    r = RelevanceRule()
    assert r.evaluate(_ctx("Yuki讲个笑话")) is None  # 无关键词命中
    assert r.evaluate(_ctx("讲个故事吧")).role == "yuki"


def test_cooldown_rule_prefers_idle():
    r = CooldownRule()
    assert r.evaluate(_ctx("随便聊聊")).role == "yuki"  # yuki 闲置更久


def test_random_rule_never_none():
    r = RandomRule(seed=1)
    verdict = r.evaluate(_ctx("随便聊聊"))
    assert verdict.role in {"yuki", "lilith"}
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_collab_rules.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现**（新建 `src/orchestrators/collaboration/rules.py`）

```python
"""rules.py — 发言权仲裁规则集（可插拔）。

优先级链（config collaboration.rules_order 可调）：
mention(@指定,硬放行) > intent(系统意图→lead) > relevance(关键词加权) >
cooldown(闲置最久) > random(平局随机 + 记录取反)。
零 LLM：全部为确定性文本规则。
"""
import random
import re
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class RuleVerdict:
    role: Optional[str]
    confidence: float
    reason: str


@dataclass
class ArbitrationContext:
    text: str
    user_name: str
    source: str
    kind: str                 # danmaku / collab / active
    lead_role: str
    present_roles: set
    profiles: object          # CharacterProfileLoader 兼容接口（keywords_for）
    turn_tracker: object      # idle_seconds(role) -> float


class Rule:
    name = "base"

    def evaluate(self, ctx: ArbitrationContext) -> RuleVerdict:
        raise NotImplementedError


_MENTION_RE = re.compile(r"@(yuki|lilith)|(yuki|yuki酱|lilith|莉莉丝)\s*(?:你看|你怎么看|你同意|讲|说|来)", re.IGNORECASE)
_INTENT_WORDS = {"下播", "开播", "状态", "感谢", "点歌", "日程", "晚安", "再见"}
_MENTION_LANG = {"yuki": {"yuki", "yuki酱"}, "lilith": {"lilith", "莉莉丝"}}


class MentionRule(Rule):
    """手动指定（硬放行）：@角色 或 "角色+你看/怎么说" 显式指向。"""
    name = "mention"

    def evaluate(self, ctx):
        low = ctx.text.lower()
        m = _MENTION_RE.search(ctx.text)
        if not m:
            return RuleVerdict(None, 0.0, "no-mention")
        hit = None
        for role in ctx.present_roles:
            for alias in _MENTION_LANG.get(role, {role}):
                if alias.lower() in low:
                    hit = role
                    break
            if hit:
                break
        if hit is None:
            return RuleVerdict(None, 0.0, "mention-unknown")
        return RuleVerdict(hit, 1.0, f"mention:{hit}")


class IntentRule(Rule):
    """系统/运营意图 → lead_role。"""
    name = "intent"

    def evaluate(self, ctx):
        low = ctx.text.strip().lower()
        if low.startswith("!"):
            return RuleVerdict(ctx.lead_role, 0.9, f"command:{low}")
        for w in _INTENT_WORDS:
            if w in ctx.text:
                return RuleVerdict(ctx.lead_role, 0.8, f"intent:{w}")
        return RuleVerdict(None, 0.0, "no-intent")


class RelevanceRule(Rule):
    """相关性：patterns > topics > personality 加权。"""
    name = "relevance"

    def evaluate(self, ctx):
        scores: Dict[str, float] = {}
        for role in ctx.present_roles:
            kw = ctx.profiles.keywords_for(role) or {}
            score = 0.0
            for pat in kw.get("patterns", []):
                if pat.startswith("regex:"):
                    if re.search(pat[len("regex:"):], ctx.text):
                        score += 3.0
                elif pat in ctx.text:
                    score += 3.0
            for topic in kw.get("topics", []):
                if topic and topic in ctx.text:
                    score += 2.0
            for tag in kw.get("personality", []):
                if tag and tag in ctx.text:
                    score += 1.0
            scores[role] = score
        top = max(scores.values(), default=0.0)
        if top <= 0:
            return RuleVerdict(None, 0.0, "no-keyword-hit")
        winners = [r for r, s in scores.items() if s == top]
        if len(winners) > 1:
            return RuleVerdict(None, top, "tie")
        return RuleVerdict(winners[0], top, f"relevance:{winners[0]}")


class CooldownRule(Rule):
    """冷却：谁闲置最久谁先说话。"""
    name = "cooldown"

    def evaluate(self, ctx):
        idle = {r: ctx.turn_tracker.idle_seconds(r) for r in ctx.present_roles}
        if not idle:
            return RuleVerdict(None, 0.0, "no-role")
        max_idle = max(idle.values())
        if max_idle <= 0:
            return RuleVerdict(None, 0.0, "all-hot")
        winners = [r for r, v in idle.items() if v == max_idle]
        return RuleVerdict(winners[0], 0.6, f"cooldown:{winners[0]}")


class RandomRule(Rule):
    """随机扰动：平局兜底；记录上次选择，下次偏向另一角色。"""
    name = "random"

    def __init__(self, seed: Optional[int] = None):
        self._rng = random.Random(seed)
        self.last_choice: Optional[str] = None

    def evaluate(self, ctx):
        roles = sorted(ctx.present_roles)
        if not roles:
            return RuleVerdict(None, 0.0, "no-role")
        if self.last_choice and len(roles) > 1:
            others = [r for r in roles if r != self.last_choice]
            choice = self._rng.choice(others)
        else:
            choice = self._rng.choice(roles)
        self.last_choice = choice
        return RuleVerdict(choice, 0.5, f"random:{choice}")


def build_default_rules(seed: Optional[int] = None) -> List[Rule]:
    return [MentionRule(), IntentRule(), RelevanceRule(),
            CooldownRule(), RandomRule(seed=seed)]


def make_rules_by_order(names: List[str]) -> List[Rule]:
    pool = {r.name: r for r in build_default_rules()}
    return [pool[n] for n in names if n in pool]
```

（`src/orchestrators/collaboration/__init__.py` 内容：空文件，或 `from .coordinator import CollaborationCoordinator` 占位——Task 14 时补充。）

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_collab_rules.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/orchestrators/collaboration/rules.py src/orchestrators/collaboration/__init__.py tests/test_collab_rules.py
git commit -m "feat(collab): 仲裁规则集（mention/intent/relevance/cooldown/random，零 LLM）"
```

---

### Task 10: collaboration/turn_tracker.py（话轮追踪）

**Files:**
- Create: `src/orchestrators/collaboration/turn_tracker.py`
- Create: `tests/test_collab_turn_tracker.py`

- [ ] **Step 1: 写失败测试**（新建 `tests/test_collab_turn_tracker.py`）

```python
"""turn_tracker 单测。"""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.collaboration.turn_tracker import TurnTracker


def test_acquire_release_mutex():
    tt = TurnTracker()
    assert tt.acquire("yuki") is True
    assert tt.acquire("lilith") is False   # 互斥
    tt.release("yuki")
    assert tt.acquire("lilith") is True


def test_idle_seconds():
    tt = TurnTracker()
    tt.acquire("yuki")
    time.sleep(0.02)
    tt.release("yuki")
    assert tt.idle_seconds("lilith") >= tt.idle_seconds("yuki")


def test_pending_queue_priority():
    tt = TurnTracker()
    tt.acquire("yuki")
    tt.enqueue({"role": "lilith", "priority": 3, "request_id": "a"})
    tt.enqueue({"role": "yuki", "priority": 0, "request_id": "b"})   # 高优插队
    tt.enqueue({"role": "lilith", "priority": 3, "request_id": "c"})
    tt.release("yuki")
    nxt = tt.dequeue()
    assert nxt["request_id"] == "b"   # P0 优先于先到的 P3


def test_history_records_turns():
    tt = TurnTracker()
    tt.record_turn("yuki", "danmaku", ref_text="")
    tt.record_turn("lilith", "banter", ref_text="哈哈")
    hist = tt.turn_history()
    assert hist[-1]["role"] == "lilith" and hist[-1]["kind"] == "banter"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_collab_turn_tracker.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现**（新建 `src/orchestrators/collaboration/turn_tracker.py`）

```python
"""turn_tracker.py — 话轮追踪：谁在说 / 待发队列（优先级插队 + FIFO）/ 冷却 / 话轮历史。"""
import heapq
import threading
import time
from typing import Any, Dict, List, Optional


class TurnTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._current: Optional[str] = None
        self._queue: List[tuple] = []          # (priority, seq, request)
        self._seq = 0
        self._last_speech: Dict[str, float] = {}
        self._history: List[Dict[str, Any]] = []

    def acquire(self, role: str) -> bool:
        with self._lock:
            if self._current is not None:
                return False
            self._current = role
            return True

    def release(self, role: str) -> None:
        with self._lock:
            if self._current == role:
                self._current = None
                self._last_speech[role] = time.time()

    @property
    def current_speaker(self) -> Optional[str]:
        with self._lock:
            return self._current

    def enqueue(self, request: Dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            heapq.heappush(self._queue,
                           (request.get("priority", 5), self._seq, request))

    def dequeue(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._current is not None or not self._queue:
                return None
            _, _, request = heapq.heappop(self._queue)
            self._current = request.get("role")
            return request

    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)

    def idle_seconds(self, role: str) -> float:
        with self._lock:
            last = self._last_speech.get(role)
        return (time.time() - last) if last else float("inf")

    def record_turn(self, role: str, kind: str, ref_text: str = "",
                    text: str = "") -> None:
        with self._lock:
            self._last_speech[role] = time.time()
            self._history.append({"role": role, "kind": kind,
                                  "ref_text": ref_text, "text": text,
                                  "ts": time.time()})
            if len(self._history) > 200:
                self._history = self._history[-200:]

    def turn_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._history[-limit:])
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_collab_turn_tracker.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/orchestrators/collaboration/turn_tracker.py tests/test_collab_turn_tracker.py
git commit -m "feat(collab): 话轮追踪（互斥/优先级队列/冷却/历史）"
```

---

### Task 11: collaboration/arbitrator.py（仲裁器）

**Files:**
- Create: `src/orchestrators/collaboration/arbitrator.py`
- Create: `tests/test_collab_arbitrator.py`

- [ ] **Step 1: 写失败测试**（新建 `tests/test_collab_arbitrator.py`）

```python
"""仲裁器单测：规则链 + 互斥 + 排队。"""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.shared.event_bus import EventBus
from src.orchestrators.collaboration.arbitrator import SpeakerArbitrator
from src.orchestrators.collaboration.turn_tracker import TurnTracker
from src.orchestrators.collaboration.rules import ArbitrationContext


class FakeProfiles:
    def keywords_for(self, role):
        return {"yuki": {"topics": ["故事"], "patterns": []},
                "lilith": {"topics": ["直播"], "patterns": []}}[role]


def _make():
    bus = EventBus()
    tt = TurnTracker()
    arb = SpeakerArbitrator(bus, tt, profiles=FakeProfiles(), lead_role="yuki")
    return bus, tt, arb


def test_arbitrate_mention():
    bus, tt, arb = _make()
    verdict = arb.arbitrate("danmaku", "@Lilith 你说呢", "观众", kind="danmaku")
    assert verdict.role == "lilith"
    assert verdict.rule_hit == "mention:lilith"


def test_arbitrate_queues_while_speaking():
    bus, tt, arb = _make()
    tt.acquire("yuki")                       # 模拟 yuki 正在发言
    verdict = arb.arbitrate("danmaku", "@Lilith 你说呢", "观众", kind="danmaku")
    assert verdict.role is None              # 未放行（互斥）
    assert tt.pending_count() == 1
    tt.release("yuki")
    popped = tt.dequeue()
    assert popped["role"] == "lilith"


def test_arbitrate_publishes_event():
    bus, tt, arb = _make()
    got = []
    bus.subscribe("speech:arbitrated", lambda **kw: got.append(kw))
    arb.arbitrate("danmaku", "随便聊聊", "观众", kind="danmaku")
    assert got and got[0]["role"] in {"yuki", "lilith"}
    assert got[0]["rule_hit"]
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_collab_arbitrator.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现**（新建 `src/orchestrators/collaboration/arbitrator.py`）

```python
"""arbitrator.py — 发言权仲裁器（核心）。

arbitrate(request) → 依次应用规则链；当前有人发言时请求入待发队列。
发布 speech:arbitrated（role/rule_hit/request_id）供前端展示与调试。
零 LLM：规则全部为确定性文本规则（rules.py）。
"""
import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from src.orchestrators.collaboration.rules import (
    ArbitrationContext, Rule, build_default_rules, make_rules_by_order,
)
from src.orchestrators.collaboration.turn_tracker import TurnTracker
from src.shared.events import SPEECH_ARBITRATED

logger = logging.getLogger(__name__)


@dataclass
class ArbitrationVerdict:
    role: Optional[str]
    rule_hit: str = ""
    request_id: str = ""


class SpeakerArbitrator:
    def __init__(self, event_bus, turn_tracker: Optional[TurnTracker] = None,
                 profiles=None, lead_role: str = "yuki",
                 rules_order: Optional[List[str]] = None, seed: Optional[int] = None):
        self._event_bus = event_bus
        self._tt = turn_tracker or TurnTracker()
        self._profiles = profiles
        self._lead_role = lead_role
        self._rules: List[Rule] = (make_rules_by_order(rules_order)
                                   if rules_order else build_default_rules(seed=seed))

    def set_profiles(self, profiles) -> None:
        self._profiles = profiles

    def set_lead_role(self, role: str) -> None:
        self._lead_role = role

    def arbitrate(self, source: str, text: str, user_name: str = "",
                  kind: str = "danmaku", requester_role: str = "",
                  ref_text: str = "") -> ArbitrationVerdict:
        request_id = uuid.uuid4().hex[:8]
        present = self._present_roles()
        ctx = ArbitrationContext(text=text, user_name=user_name, source=source,
                                 kind=kind, lead_role=self._lead_role,
                                 present_roles=present, profiles=self._profiles,
                                 turn_tracker=self._tt)
        winner = None
        hit = ""
        for rule in self._rules:
            verdict = rule.evaluate(ctx)
            if verdict.role is not None:
                winner = verdict.role
                hit = verdict.reason
                break
        if winner is None:
            return ArbitrationVerdict(None, hit, request_id)

        request = {"role": winner, "priority": self._priority(kind, hit),
                   "request_id": request_id, "text": text, "ref_text": ref_text}
        if not self._tt.acquire(winner):
            self._tt.enqueue(request)     # 有人正发言：入队等待
            self._publish(winner, hit, request_id, deferred=True)
            return ArbitrationVerdict(None, hit, request_id)
        self._publish(winner, hit, request_id, deferred=False)
        return ArbitrationVerdict(winner, hit, request_id)

    @staticmethod
    def _priority(kind: str, hit: str) -> int:
        if hit.startswith("mention"):
            return 0
        if hit.startswith("intent") or hit.startswith("command"):
            return 1
        if hit.startswith("relevance"):
            return 2
        if kind == "collab":
            return 3
        if kind == "active":
            return 4
        return 5

    def _present_roles(self) -> set:
        if self._profiles is not None and hasattr(self._profiles, "all_roles"):
            return set(self._profiles.all_roles())
        return {"yuki", "lilith"}

    def _publish(self, role: str, rule_hit: str, request_id: str,
                 deferred: bool) -> None:
        try:
            self._event_bus.publish(SPEECH_ARBITRATED, role=role,
                                    rule_hit=rule_hit, request_id=request_id,
                                    deferred=deferred)
        except Exception as e:
            logger.warning("[Arbitrator] 发布仲裁事件失败: %s", e)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_collab_arbitrator.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/orchestrators/collaboration/arbitrator.py tests/test_collab_arbitrator.py
git commit -m "feat(collab): 发言权仲裁器（规则链 + 互斥 + 优先级排队）"
```

---

### Task 12: collaboration/context_manager.py（多角色上下文）

**Files:**
- Create: `src/orchestrators/collaboration/context_manager.py`
- Create: `tests/test_collab_context.py`

- [ ] **Step 1: 写失败测试**（新建 `tests/test_collab_context.py`）

```python
"""context_manager 单测。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.collaboration.context_manager import ContextManager


def test_memory_key_buckets_by_character():
    cm = ContextManager()
    assert cm.memory_key("yuki") == "default:yuki"
    assert cm.memory_key("lilith") != cm.memory_key("yuki")


def test_global_transcript_ring():
    cm = ContextManager(max_transcript=3)
    cm.record_turn("yuki", "hello yuki")
    cm.record_turn("lilith", "hello lilith")
    cm.record_turn("yuki", "third")
    cm.record_turn("lilith", "fourth")
    lines = cm.global_transcript()
    assert len(lines) == 3
    assert "fourth" in lines[-1] and "hello yuki" not in lines


def test_system_prompt_with_awareness():
    cm = ContextManager(max_partner_lines=2)
    cm.record_turn("lilith", "你刚才讲的故事不错")
    prompt = cm.build_system_prompt("yuki", partner_lines="你的搭档Lilith刚才说：...")
    assert "你的搭档Lilith" in prompt
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_collab_context.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现**（新建 `src/orchestrators/collaboration/context_manager.py`）

```python
"""context_manager.py — 多角色上下文：每角色独立记忆分桶 + 全局对话流 + 感知彼此注入。"""
from typing import List, Optional


class ContextManager:
    def __init__(self, session_id: str = "default", max_transcript: int = 50,
                 max_partner_lines: int = 2):
        self._session_id = session_id
        self._max_transcript = max_transcript
        self._max_partner_lines = max_partner_lines
        self._transcript: List[str] = []

    def memory_key(self, role: str) -> str:
        """记忆分桶键（配合 memory:retrieve/store 的 character_id）。"""
        return f"{self._session_id}:{role}"

    def record_turn(self, role: str, text: str) -> None:
        if text:
            self._transcript.append(f"{role}: {text}")
            if len(self._transcript) > self._max_transcript:
                self._transcript = self._transcript[-self._max_transcript:]

    def global_transcript(self, limit: int = 0) -> List[str]:
        n = limit or self._max_transcript
        return list(self._transcript[-n:])

    def partner_lines(self, speaker: str, limit: int = 0) -> List[str]:
        """对方最近发言（感知彼此数据源）。"""
        n = limit or self._max_partner_lines
        partner = [ln for ln in self._transcript
                   if not ln.startswith(speaker + ":")]
        return partner[-n:]

    def build_system_prompt(self, role: str, base_prompt: str,
                            awareness_enabled: bool = True,
                            partner_lines: Optional[List[str]] = None) -> str:
        prompt = base_prompt
        if awareness_enabled:
            lines = partner_lines if partner_lines is not None else self.partner_lines(role)
            if lines:
                prompt += "\n\n【感知彼此】对方最近发言：\n" + "\n".join(lines)
        return prompt.strip()
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_collab_context.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/orchestrators/collaboration/context_manager.py tests/test_collab_context.py
git commit -m "feat(collab): 多角色上下文（记忆分桶/全局流/感知彼此注入）"
```

---

### Task 13: collaboration/triggers.py（联动触发）

**Files:**
- Create: `src/orchestrators/collaboration/triggers.py`
- Create: `tests/test_collab_triggers.py`

- [ ] **Step 1: 写失败测试**（新建 `tests/test_collab_triggers.py`）

```python
"""triggers 单测。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.collaboration.triggers import CollabTriggers


def test_banter_proposal_after_speech():
    tr = CollabTriggers(probability=1.0, global_cooldown=0.0,
                        present_roles={"yuki", "lilith"})
    props = tr.evaluate("yuki", "今天讲个故事吧")
    assert props and props[0]["role"] == "lilith" and props[0]["kind"] == "banter"


def test_global_cooldown_blocks():
    tr = CollabTriggers(probability=1.0, global_cooldown=3600.0,
                        present_roles={"yuki", "lilith"})
    tr.evaluate("yuki", "第一条")
    props = tr.evaluate("lilith", "第二条")   # 冷却期内
    assert props == []


def test_probability_zero_disables():
    tr = CollabTriggers(probability=0.0, global_cooldown=0.0,
                        present_roles={"yuki", "lilith"})
    assert tr.evaluate("yuki", "随便") == []
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_collab_triggers.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现**（新建 `src/orchestrators/collaboration/triggers.py`）

```python
"""triggers.py — 联动触发条件：发言完成后决定是否让另一角色接话（banter）。

产出 TriggerProposal → coordinator.request_utterance → 回仲裁器（冷却/互斥约束下放行）。
"""
import logging
import random
import time
from typing import Dict, List

logger = logging.getLogger(__name__)


class CollabTriggers:
    def __init__(self, probability: float = 0.3, global_cooldown: float = 20.0,
                 present_roles=None, seed: Optional[int] = None):
        self._probability = float(probability)
        self._cooldown = float(global_cooldown)
        self._present = set(present_roles or {"yuki", "lilith"})
        self._rng = random.Random(seed)
        self._last_trigger_at = 0.0

    def update_runtime(self, probability: float, global_cooldown: float,
                       present_roles=None) -> None:
        self._probability = float(probability)
        self._cooldown = float(global_cooldown)
        if present_roles:
            self._present = set(present_roles)

    def evaluate(self, speaker: str, text: str) -> List[Dict[str, str]]:
        """发言完成后调用；返回接话提案列表（通常 0 或 1 条）。"""
        now = time.time()
        if now - self._last_trigger_at < self._cooldown:
            return []
        if self._probability <= 0 or self._rng.random() > self._probability:
            return []
        others = [r for r in sorted(self._present) if r != speaker]
        if not others:
            return []
        self._last_trigger_at = now
        target = others[0]
        return [{"role": target, "kind": "banter", "reason": "speech-completed",
                 "ref_text": text}]


# 兼容 typing 引用
Optional = None
```

> 注：文件顶部 `from typing import Optional` 正常引入即可（占位注释仅为计划简洁；实现时保留真实 import）。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_collab_triggers.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/orchestrators/collaboration/triggers.py tests/test_collab_triggers.py
git commit -m "feat(collab): 联动触发（接话概率 + 全局冷却）"
```

---

### Task 14: collaboration/coordinator.py（顶层协调器）

**Files:**
- Create: `src/orchestrators/collaboration/coordinator.py`
- Create: `tests/test_collab_coordinator.py`

- [ ] **Step 1: 写失败测试**（新建 `tests/test_collab_coordinator.py`）

```python
"""coordinator 单测（mock pipeline，不依赖真实 LLM/TTS）。"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.shared.event_bus import EventBus
from src.orchestrators.collaboration.coordinator import CollaborationCoordinator


class FakeProfiles:
    def all_roles(self):
        return ["yuki", "lilith"]

    def keywords_for(self, role):
        return {"yuki": {"topics": ["故事"], "patterns": []},
                "lilith": {"topics": ["直播"], "patterns": []}}[role]


class FakePipeline:
    def __init__(self):
        self.calls = []

    def execute_with(self, text, role, system_prompt="", turn_context=None):
        self.calls.append({"text": text, "role": role,
                           "prompt_has_persona": bool(system_prompt)})
        return {"ok": True, "data": {"reply": "ok", "audio_id": "a1"}}


def test_danmaku_arbitrates_and_executes():
    bus = EventBus()
    pipeline = FakePipeline()
    co = CollaborationCoordinator(bus, pipeline=pipeline, profiles=FakeProfiles(),
                                  trigger_probability=0.0, awareness_enabled=True)
    co.start()
    bus.publish("danmaku:received", content="@Lilith 你同意吗", user_name="观众甲")
    co.flush()   # 同步队列处理
    assert pipeline.calls and pipeline.calls[0]["role"] == "lilith"
    assert pipeline.calls[0]["prompt_has_persona"] is True
    co.stop()


def test_speech_completed_triggers_banter():
    bus = EventBus()
    pipeline = FakePipeline()
    co = CollaborationCoordinator(bus, pipeline=pipeline, profiles=FakeProfiles(),
                                  trigger_probability=1.0, trigger_global_cooldown=0.0,
                                  awareness_enabled=True)
    co.start()
    bus.publish("danmaku:received", content="讲个故事吧", user_name="观众甲")
    co.flush()
    assert len(pipeline.calls) >= 1
    # 触发接话：发言完成后另一方提案（冷却为 0 且概率 1 → 应出现接话调用）
    assert len(pipeline.calls) >= 2
    co.stop()
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_collab_coordinator.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现**（新建 `src/orchestrators/collaboration/coordinator.py`）

```python
"""coordinator.py — 多角色协作顶层协调器。

组装 arbitrator/turn_tracker/context_manager/triggers，订阅事件并驱动
DanmakuPipeline.execute_with 按角色执行。对外统一接口：
handle_danmaku / handle_speech_completed / request_utterance / snapshot / start / stop。
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

from src.orchestrators.collaboration.arbitrator import SpeakerArbitrator
from src.orchestrators.collaboration.context_manager import ContextManager
from src.orchestrators.collaboration.triggers import CollabTriggers
from src.orchestrators.collaboration.turn_tracker import TurnTracker
from src.shared.events import (
    COLLAB_UTTERANCE_REQUESTED,
    DANMAKU_RECEIVED,
    SPEECH_COMPLETED,
)

logger = logging.getLogger(__name__)


class CollaborationCoordinator:
    def __init__(self, event_bus, pipeline=None, profiles=None,
                 session=None, live2d=None,
                 lead_role: str = "yuki", rules_order: Optional[List[str]] = None,
                 trigger_probability: float = 0.3,
                 trigger_global_cooldown: float = 20.0,
                 awareness_enabled: bool = True, seed: Optional[int] = None):
        self._event_bus = event_bus
        self._pipeline = pipeline
        self._profiles = profiles
        self._session = session
        self._live2d = live2d
        self._tt = TurnTracker()
        self._ctx = ContextManager()
        self._arb = SpeakerArbitrator(event_bus, self._tt, profiles=profiles,
                                      lead_role=lead_role, rules_order=rules_order,
                                      seed=seed)
        self._triggers = CollabTriggers(
            probability=trigger_probability, global_cooldown=trigger_global_cooldown,
            present_roles=(set(profiles.all_roles()) if profiles else {"yuki", "lilith"}),
            seed=seed)
        self._awareness = awareness_enabled
        self._runtime = {"trigger_probability": float(trigger_probability),
                         "trigger_global_cooldown": float(trigger_global_cooldown),
                         "lead_role": lead_role,
                         "awareness_enabled": bool(awareness_enabled),
                         "rules_order": list(rules_order or
                                             ["mention", "intent", "relevance",
                                              "cooldown", "random"])}
        self._started = False

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._started:
            return
        self._event_bus.subscribe(DANMAKU_RECEIVED, self._on_danmaku)
        self._event_bus.subscribe(SPEECH_COMPLETED, self._on_speech_completed)
        self._event_bus.subscribe(COLLAB_UTTERANCE_REQUESTED, self._on_utterance_requested)
        self._started = True
        logger.info("[Collaboration] 已启动（订阅 danmaku/speech/completed/utterance）")

    def stop(self) -> None:
        if not self._started:
            return
        self._event_bus.unsubscribe(DANMAKU_RECEIVED, self._on_danmaku)
        self._event_bus.unsubscribe(SPEECH_COMPLETED, self._on_speech_completed)
        self._event_bus.unsubscribe(COLLAB_UTTERANCE_REQUESTED, self._on_utterance_requested)
        self._started = False

    # ---------- 事件入口（EventBus 同步回调） ----------

    def _on_danmaku(self, event: str, content: str, user_name: str = "", **kw) -> None:
        text = (content or "").strip()
        if not text or text.startswith("!"):
            return
        verdict = self._arb.arbitrate("danmaku", text, user_name, kind="danmaku")
        if verdict.role:
            self._execute(verdict.role, text, kind="danmaku")

    def _on_speech_completed(self, event: str, role: str, text: str = "",
                             audio_id: str = "", **kw) -> None:
        if not self._started:
            return
        self._ctx.record_turn(role, text or audio_id)
        self._tt.record_turn(role, "speech", text=text)
        props = self._triggers.evaluate(role, text)
        for p in props:
            self._event_bus.publish(COLLAB_UTTERANCE_REQUESTED, **p)

    def _on_utterance_requested(self, event: str, role: str, kind: str = "banter",
                                reason: str = "", ref_text: str = "", **kw) -> None:
        self.request_utterance(role, kind, reason, ref_text)

    # ---------- 对外接口 ----------

    def request_utterance(self, role: str, kind: str, reason: str = "",
                          ref_text: str = "") -> None:
        """联动发言请求（triggers/外部调用）→ 仲裁 → 执行。"""
        verdict = self._arb.arbitrate("collab", ref_text or "接个话", "",
                                      kind="collab", requester_role=role,
                                      ref_text=ref_text)
        if verdict.role:
            self._execute(verdict.role, ref_text or "接个话", kind=kind)

    def update_runtime(self, **kwargs) -> Dict[str, Any]:
        """运行时调参（POST /api/collab/config 白名单）。"""
        allowed = {"trigger_probability", "trigger_global_cooldown",
                   "lead_role", "awareness_enabled", "rules_order"}
        for k, v in kwargs.items():
            if k in allowed:
                self._runtime[k] = v
        self._triggers.update_runtime(
            float(self._runtime["trigger_probability"]),
            float(self._runtime["trigger_global_cooldown"]))
        self._arb.set_lead_role(str(self._runtime["lead_role"]))
        return dict(self._runtime)

    def snapshot(self) -> Dict[str, Any]:
        return {"enabled": self._started,
                "current_speaker": self._tt.current_speaker,
                "pending": self._tt.pending_count(),
                "runtime": dict(self._runtime),
                "recent_turns": self._tt.turn_history(limit=10)}

    def flush(self, timeout: float = 2.0) -> None:
        """测试辅助：等待异步执行排空（生产不使用）。"""
        deadline = __import__("time").time() + timeout
        while __import__("time").time() < deadline and self._tt.pending_count() > 0:
            __import__("time").sleep(0.01)

    # ---------- 内部 ----------

    def _execute(self, role: str, text: str, kind: str = "danmaku") -> None:
        if self._pipeline is None:
            logger.warning("[Collaboration] pipeline 未注入，跳过执行 role=%s", role)
            return
        base_prompt = ""
        if self._profiles is not None:
            p = self._profiles.load(role)
            base_prompt = p.system_prompt if p else ""
        turn_context = self._ctx.global_transcript()
        self._tt.record_turn(role, kind, text=text)
        self._tt.release(role)      # 释放互斥（执行异步，先放行队列）
        try:
            asyncio.run(self._pipeline.execute_with(
                text=text, role=role,
                system_prompt=self._ctx.build_system_prompt(
                    role, base_prompt, self._runtime["awareness_enabled"]),
                turn_context=turn_context))
        except Exception as e:
            logger.error("[Collaboration] execute_with 异常: %s", e)
```

> 注意：`_execute` 内 `_tt.release(role)` 与 `arbitrate` 的 `acquire` 配对，保证互斥只在仲裁→派发瞬间持有，避免阻塞后续请求；`dequeue` 由 `release` 后调用方（本实现为简化：`arbitrate` 在互斥忙时入队，`release` 后由下一次 `arbitrate` 出队；生产可加定时 flush 待发队列——Task 16 装配处增加 watchdog 式周期 `drain`，若需严格即时则在此 release 后立即 dequeue 并执行，计划按"释放后立即排空"实现）：

补充 `_execute` 内 release 后立即排空：

```python
        # 释放后立即处理待发队列（最高优先级者先行）
        nxt = self._tt.dequeue()
        while nxt is not None:
            nxt_role = nxt.get("role")
            nxt_text = nxt.get("text") or nxt.get("ref_text") or ""
            nxt_kind = nxt.get("kind", "danmaku")
            self._tt.release(nxt_role)
            self._execute(nxt_role, nxt_text, nxt_kind)
            nxt = self._tt.dequeue()
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_collab_coordinator.py -v`
Expected: PASS（若递归调用时序不稳，将 `_execute` 内递归排空改为 `dequeue` 循环 + `_run_utterance` 独立函数）

- [ ] **Step 5: 提交**

```bash
git add src/orchestrators/collaboration/coordinator.py tests/test_collab_coordinator.py
git commit -m "feat(collab): 顶层协调器（事件接线 + 按角色执行 + 运行时调参）"
```

---

### Task 15: DanmakuPipeline.execute_with（链路参数化）

**Files:**
- Modify: `src/commander/danmaku_pipeline.py`
- Modify: `tests/test_danmaku_pipeline.py`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_danmaku_pipeline.py`）

```python
def test_execute_with_uses_role_and_publishes_completed():
    import asyncio
    from src.shared.event_bus import EventBus
    from src.commander.danmaku_pipeline import DanmakuPipeline

    class FakeLLM:
        async def handle(self, cmd):
            return {"ok": True, "data": {"reply": "回复内容"}}

    class FakeTTS:
        async def handle(self, cmd):
            return {"ok": True, "data": {"audio_id": "a1"}}

    class FakeSafety:
        async def handle(self, cmd):
            return {"ok": True, "data": {"verdict": "allow"}}

    bus = EventBus()
    pipe = DanmakuPipeline(bus, llm_orchestrator=FakeLLM(),
                           tts_orchestrator=FakeTTS(),
                           safety_orchestrator=FakeSafety())
    events = []
    bus.subscribe("speech:completed", lambda **kw: events.append(kw))
    bus.subscribe("frontend:subtitle_update", lambda **kw: events.append(kw))
    asyncio.run(pipe.execute_with("你好", role="lilith",
                                  system_prompt="你是Lilith", turn_context=[]))
    assert events and events[0]["type"] == "frontend:subtitle_update"
    assert events[0]["role"] == "lilith"
    assert events[-1]["type"] == "speech:completed"
    assert events[-1]["role"] == "lilith"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_danmaku_pipeline.py::test_execute_with_uses_role_and_publishes_completed -v`
Expected: FAIL（AttributeError: execute_with）

- [ ] **Step 3: 实现**（`src/commander/danmaku_pipeline.py`）

```python
    def execute_with(self, text: str, role: str, system_prompt: str = "",
                     turn_context=None, user_name: str = "") -> Dict[str, Any]:
        """参数化入口：按指定角色 + 人格 + 话轮上下文执行全链路。

        单角色模式默认路径（_on_danmaku）调用本方法（role=_current_role()）保持一致。
        """
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "text 必填"}
        try:
            return asyncio.run(self._process(text, role, system_prompt,
                                             turn_context or [], user_name))
        except Exception as e:
            logger.error("[DanmakuPipeline] execute_with 异常: %s", e)
            return {"ok": False, "error": str(e)}
```

同步改造 `_process` 签名与内部（字幕/LLM/TTS 均用传入 role，`_chat` 注入 system_prompt，末尾发布 `speech:completed`）：

```python
    async def _process(self, text: str, role: str, system_prompt: str = "",
                       turn_context=None, user_name: str = "") -> Dict[str, Any]:
        turn_context = turn_context or []
        if not await self._check_input(text):
            return {"ok": False, "error": "input-blocked"}

        history = await self._retrieve_memory(text, role)

        reply_text = await self._chat(text, history, role, system_prompt,
                                      turn_context)
        if not reply_text:
            return {"ok": False, "error": "llm-empty"}

        if not await self._check_output(reply_text):
            return {"ok": False, "error": "output-blocked"}

        self._event_bus.publish(FRONTEND_SUBTITLE_UPDATE,
                                text=reply_text, role=role,
                                user_name=user_name)

        synth = await self._synthesize(reply_text, role)
        audio_id = synth.get("audio_id", "") if synth else ""

        await self._store_memory(text, reply_text, role)

        self._event_bus.publish(SPEECH_COMPLETED, role=role, text=reply_text,
                                audio_id=audio_id)
        return {"ok": True, "data": {"reply": reply_text, "audio_id": audio_id},
                "error": None}

    async def _retrieve_memory(self, text: str, role: str = "") -> List[Dict[str, str]]:
        # 仅修改 memory:retrieve payload：加 character_id=role
        payload = {"query": text, "k": 3, "session_id": SESSION_ID}
        if role:
            payload["character_id"] = role
        # ... 其余与现状一致 ...

    async def _chat(self, text: str, history, role: str = "", system_prompt: str = "",
                    turn_context=None) -> str:
        payload = {"text": text, "role": role, "history": history}
        if system_prompt:
            payload["system_prompt"] = system_prompt
        if turn_context:
            payload["turn_context"] = turn_context
        result = await self._llm.handle({"capability": "llm:chat",
                                         "payload": payload})
        # ... 其余与现状一致 ...

    async def _synthesize(self, reply_text: str, role: str = "") -> Optional[Dict]:
        if self._tts is None:
            return None
        capability = "tts:stream_synthesize" if len(reply_text) > 80 else "tts:synthesize"
        result = await self._tts.handle({
            "capability": capability,
            "payload": {"text": reply_text, "role": role or self._current_role()},
        })
        return result.get("data", {}) if result.get("ok") else None

    async def _store_memory(self, text: str, reply_text: str, role: str = "") -> None:
        # memory:store payload 增加 character_id=role（两段均加）
        # ... 其余与现状一致 ...

    def _on_danmaku(self, event, content, user_name="", **kwargs):
        # 默认路径改为调用 execute_with（role=_current_role()），保持单角色行为一致
        text = (content or "").strip()
        if not text or text.startswith("!"):
            return
        if self._llm is None:
            return
        self.execute_with(text, role=self._current_role(), user_name=user_name)
```

`import` 处新增 `SPEECH_COMPLETED`。注意：`_process` 中 `_store_memory` 现有 `role` 语义为"user/assistant"消息角色，勿与发言角色混淆——`character_id=role`（发言角色）与 `role="user"/"assistant"`（消息角色）是不同字段。

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_danmaku_pipeline.py -v`
Expected: PASS

Run: `python -m pytest -q`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add src/commander/danmaku_pipeline.py tests/test_danmaku_pipeline.py
git commit -m "feat(pipeline): execute_with 参数化入口（角色/人格/话轮）+ speech:completed"
```

---

### Task 16: app.py 装配 + POST /api/collab/config

**Files:**
- Modify: `src/app.py`
- Modify: `src/web/app_factory.py`
- Modify: `src/orchestrators/tts_orchestrator/tts_orchestrator.py`（TTS_AUDIO_READY 补 role）
- Create: `tests/test_p2_app_boot.py`（追加）

- [ ] **Step 1: TTS 事件补 role**（`tts_orchestrator.py` `_synthesize` 与 `_stream_synthesize`）

```python
        self._event_bus.publish(TTS_AUDIO_READY, audio_id=audio_id,
                                duration_ms=duration_ms, path=str(audio_path),
                                role=role)
```

（两处 `TTS_AUDIO_READY` 发布点均加 `role=role`；`role` 变量两函数内已存在。）

- [ ] **Step 2: 写失败测试**（追加 `tests/test_p2_app_boot.py`）

```python
def test_app_boot_with_collaboration_disabled():
    """collaboration.enabled=false 默认：不装配协作协调器，单角色行为不变。"""
    from src.app import build_app_context
    app, bus = build_app_context()
    ctx = app.config["APP_CONTEXT"]
    assert ctx.get("collaboration") is None
    assert ctx["session"].present_roles == {"yuki"}


def test_app_boot_with_collaboration_enabled():
    import os
    os.environ["COLLAB_ENABLED"] = "1"
    from src.app import build_app_context
    app, bus = build_app_context()
    ctx = app.config["APP_CONTEXT"]
    assert ctx.get("collaboration") is not None
    assert ctx["session"].present_roles == {"yuki", "lilith"}
    client = app.test_client()
    resp = client.post("/api/collab/config",
                       json={"trigger_probability": 0.5})
    assert resp.status_code == 200 and resp.get_json()["trigger_probability"] == 0.5
    os.environ.pop("COLLAB_ENABLED", None)
```

- [ ] **Step 3: 运行确认失败**

Run: `python -m pytest tests/test_p2_app_boot.py::test_app_boot_with_collaboration_enabled -v`
Expected: FAIL（collaboration 未装配 / 404）

- [ ] **Step 4: 实现装配**（`src/app.py` `build_app_context`）

在 `pipeline` 创建后、`context` 组装前插入：

```python
    # ---------- 多角色协作（collaboration.enabled 开关，默认关） ----------
    collaboration = None
    if str(os.environ.get("COLLAB_ENABLED", "") or
           config_loader.get("collaboration.enabled", False)).lower() in ("1", "true"):
        from src.commander.character_profile import CharacterProfileLoader
        from src.orchestrators.collaboration.coordinator import CollaborationCoordinator

        profiles = CharacterProfileLoader()
        collab_cfg = config_loader.get("collaboration", {}) or {}
        for r in profiles.all_roles():
            session.add_role(r)
        collaboration = CollaborationCoordinator(
            event_bus=event_bus,
            pipeline=pipeline,
            profiles=profiles,
            session=session,
            live2d=registry.get("live2d"),
            lead_role=str(collab_cfg.get("lead_role", "yuki")),
            rules_order=collab_cfg.get("rules_order"),
            trigger_probability=float(collab_cfg.get("trigger_probability", 0.3)),
            trigger_global_cooldown=float(collab_cfg.get("trigger_global_cooldown", 20.0)),
            awareness_enabled=bool((collab_cfg.get("awareness") or {}).get("enabled", True)),
        )
        collaboration.start()
```

`context` 字典增加：`"collaboration": collaboration, "profiles": profiles if collaboration else None`。

`state_provider` 装配处传 `characters_provider`：

```python
    def characters_provider():
        chars = {}
        for r in session.present_roles:
            chars[r] = {"present": True}
        if collaboration is not None:
            snap = collaboration.snapshot()
            for r, item in chars.items():
                item["speaking"] = (snap.get("current_speaker") == r)
        return chars

    state_provider = StateProvider(..., characters_provider=characters_provider)
```

- [ ] **Step 5: 实现配置路由**（`src/web/app_factory.py`）

```python
    # POST /api/collab/config：多角色运行时调参（白名单，重启回落 config.yaml）
    @app.post("/api/collab/config")
    def collab_config():
        collab = context.get("collaboration")
        if collab is None:
            return jsonify({"ok": False, "error": "collaboration 未启用"}), 404
        body = request.get_json(silent=True) or {}
        allowed = {"trigger_probability", "trigger_global_cooldown",
                   "lead_role", "awareness_enabled", "rules_order"}
        update = {k: v for k, v in body.items() if k in allowed}
        if not update:
            return jsonify({"ok": False, "error": "无有效字段"}), 400
        return jsonify({"ok": True, "data": collab.update_runtime(**update)})
```

（`app_factory.py` 顶部 import 补充 `request`。）

- [ ] **Step 6: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_p2_app_boot.py -v`
Expected: PASS（若 `build_app_context` 需真实密钥，先确保 `.env` 存在——本仓库已有）

Run: `python -m pytest -q`
Expected: 全部通过

- [ ] **Step 7: 提交**

```bash
git add src/app.py src/web/app_factory.py src/orchestrators/tts_orchestrator/tts_orchestrator.py tests/test_p2_app_boot.py
git commit -m "feat(app): collaboration 装配开关 + POST /api/collab/config + TTS 事件补 role"
```

---

### Task 17: 前端 store characters + assistant 双角色

**Files:**
- Modify: `frontend/assets/store.js`
- Modify: `frontend/assistant/index.html`

- [ ] **Step 1: store.js 增加 characters 状态**（`initialState` 与快照合并）

```javascript
  function initialState() {
    return {
      seq: 0, snapshotSeq: -1, version: 0,
      session: {}, switches: {}, orchestrators: [], degradation: {},
      cost: {}, watchdog: {}, characters: {},
      events: [], commands: {}
    };
  }

  // reducer 快照分支内追加：
  next.characters = snap.characters || next.characters;
  // 事件分支追加（speech 状态更新）：
  if (event.role && next.characters[event.role]) {
    if (type === 'speech:arbitrated') next.characters[event.role].speaking = true;
    if (type === 'speech:completed') next.characters[event.role].speaking = false;
  }
```

- [ ] **Step 2: assistant 支持 @ 定向 + 角色在场展示**

`frontend/assistant/index.html`：

```javascript
  // 发送指令：解析 @角色 前缀 → target_role（走 MentionRule）
  function parseTarget(text) {
    var m = /^\s*@(yuki|lilith)[\s:：]/.exec(text);
    if (m) return { role: m[1], rest: text.slice(m[0].length) };
    return { role: "", rest: text };
  }
  function sendCommand(raw) {
    var t = parseTarget(raw);
    var payload = { text: t.rest || raw, session_id: SESSION_ID };
    if (t.role) payload.target_role = t.role;
    API.post('/api/command', payload, TIMEOUT_COMMAND).then(...);
  }
```

角色在场状态面板（store `characters` → 渲染在场/说话标识）：

```javascript
  store.subscribe('state:changed', function (state) {
    renderStatusPanel(state);
    renderCharacters(state.characters);   // 在场角色 + 谁在说（speaking 高亮）
  });
```

- [ ] **Step 3: 浏览器验收**

Run: 访问 `http://localhost:5000/assistant/`
Expected: 状态面板显示在场角色；输入 `@Lilith 你好` 后请求体带 `target_role`；角色说话时 `speaking` 高亮。

- [ ] **Step 4: 提交**

```bash
git add frontend/assets/store.js frontend/assistant/index.html
git commit -m "feat(frontend): store characters 状态 + assistant @ 定向与在场展示"
```

---

# P3 · 增强

### Task 18: 冷场自发闲聊（复用 active_dialogue）

**Files:**
- Modify: `src/orchestrators/llm_orchestrator/active_dialogue.py`
- Modify: `src/orchestrators/collaboration/coordinator.py`
- Modify: `tests/test_collab_coordinator.py`

- [ ] **Step 1: active_dialogue 事件补 role + generator 角色化**

`active_dialogue.py`：`set_generator(fn)` 扩展为 `set_generator(fn)` 兼容（fn 无参）；新增 `set_role_generator(fn)`（`fn(role)`）供多角色使用；`tick()` 支持 `role` 参数，`_generate(role)` 优先 `self._role_generator(role)`：

```python
    def set_role_generator(self, fn):
        """注入按角色生成话题的函数（fn(role) -> {text, mood}）。"""
        self._role_generator = fn

    def tick(self, role: str = "") -> Optional[Dict[str, str]]:
        # ... 既有触发逻辑不变 ...
        result = self._generate(role)
        # ...
        self._publish(ACTIVE_DIALOGUE, text=text, mood=mood, role=role,
                      source="active_dialogue", timestamp=now, count=self._active_count)
```

`coordinator.py` 增加冷场接线（start 订阅 `dialogue:active`）：

```python
    def _on_active_dialogue(self, event, text, mood="default", role="", **kw):
        """冷场闲聊：仲裁谁先说 → 按角色人格生成并发言。"""
        if role:
            self._execute(role, text, kind="active")
            return
        verdict = self._arb.arbitrate("active", text, kind="active")
        if verdict.role:
            self._execute(verdict.role, text, kind="active")
```

装配处（app.py，collaboration 开启时）注入角色生成器：

```python
        active = registry.get("llm")._active            # LLM 调度官内部 active_dialogue
        if active is not None:
            active.set_role_generator(lambda role: _gen_for_role(role, profiles, session))
```

`_gen_for_role(role, profiles, session)`：构造角色化 prompt（`你是{role}人设，对方最近发言：...`）调 LLM `_chat`，失败回退 `DEFAULT_TOPICS` 随机。

- [ ] **Step 2: 测试**（追加 `tests/test_collab_coordinator.py`）

```python
def test_active_dialogue_arbitrates_speaker():
    bus = EventBus()
    pipeline = FakePipeline()
    co = CollaborationCoordinator(bus, pipeline=pipeline, profiles=FakeProfiles(),
                                  trigger_probability=0.0, awareness_enabled=True)
    co.start()
    bus.publish("dialogue:active", text="大家晚上好呀", mood="happy")
    co.flush()
    assert pipeline.calls and pipeline.calls[0]["role"] in {"yuki", "lilith"}
    co.stop()
```

- [ ] **Step 3: 运行确认通过 + 回归**

Run: `python -m pytest tests/test_collab_coordinator.py tests/test_llm_orchestrator.py -v`
Expected: PASS

Run: `python -m pytest -q`
Expected: 全部通过

- [ ] **Step 4: 提交**

```bash
git add src/orchestrators/llm_orchestrator/active_dialogue.py src/orchestrators/collaboration/coordinator.py src/app.py tests/test_collab_coordinator.py
git commit -m "feat(collab): 冷场自发闲聊（active_dialogue 角色化 + 仲裁接线）"
```

---

### Task 19: 感知彼此强化 + 双角色 UI 打磨（收尾）

**Files:**
- Modify: `src/orchestrators/collaboration/coordinator.py`（awareness 上下文已就绪，验证注入生效）
- Modify: `frontend/live2d_stream/index.html`（说话高亮）
- Modify: `frontend/subtitle_overlay/index.html`（双角色样式）
- Modify: `config/config.yaml`（评审调参注释）

- [ ] **Step 1: 前端说话高亮 + 字幕双角色样式**

`live2d_stream/index.html`：`speech:arbitrated`/`speech:completed` 时对对应模型加高亮 class（如 `actor-speaking` 边框/聚光），`speech:completed` 移除。

`subtitle_overlay/index.html`：`#subtitle-role` 按 `event.role` 设置角色名与主题色（yuki 紫 / lilith 红，复用 `tokens.css` 变量）。

- [ ] **Step 2: 配置注释与默认值复核**

`config/config.yaml` collaboration 域补充注释（初值说明、rules_order 含义）。

- [ ] **Step 3: 完整回归 + 端到端验收**

Run: `python -m pytest -q`
Expected: 全部通过

Run: 开启 `COLLAB_ENABLED=1` 启动 `python src/app.py`，执行既有端到端脚本（API/四页面/WS/角色切换/断线恢复）+ 双人对话手工验收：
- [ ] 双模型同屏，Yuki（Hiyori）静默呼吸受限
- [ ] `@Lilith 你好` → Lilith 回应（MentionRule）
- [ ] 弹幕"讲个故事" → Yuki 回应（RelevanceRule）
- [ ] 发言完成后对方在冷却外接话（banter）
- [ ] `POST /api/collab/config {"trigger_probability": 0.8}` 生效，接话变频繁
- [ ] 冷场（静默 > 阈值）触发双人自发闲聊
- [ ] `collaboration.enabled=false` 重启后单角色行为与改造前一致

- [ ] **Step 4: 提交**

```bash
git add frontend/live2d_stream/index.html frontend/subtitle_overlay/index.html config/config.yaml
git commit -m "feat(ui): 说话高亮 + 双角色字幕样式 + 配置注释（多角色功能完成）"
```

---

## 自审记录

- **规格覆盖**：在场模型（T2/T5）✓；联动模块组 6 文件（T9-T14）✓；execute_with（T15）✓；装配与开关（T16）✓；事件契约与 role 透传（T1/T7/T16）✓；双模型渲染 + 呼吸限制 + 映射（T6/T7/T8）✓；关键词格式与兜底（T3）✓；运行时调参（T16）✓；回归范围（T19 Step3）✓；冷场闲聊（T18）✓；队列优先级（T10/T11）✓；口型收尾（T8）✓。
- **占位符**：无 TBD/TODO；Task 13 注释中的"Optional 占位"已注明实现时用真实 import。
- **类型一致**：`execute_with(text, role, system_prompt, turn_context)` 在 T14/T15/T18 调用签名一致；`speech:completed(role, text, audio_id)` 在 T8/T14/T15 一致；`arbitrate(source, text, user_name, kind, requester_role, ref_text)` 在 T11/T14/T18 一致；`update_runtime(**kwargs)` 在 T14/T16 一致。
