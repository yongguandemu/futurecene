# 任务三：Live2D 本地模型驱动（情绪+动作+口型）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Live2D 从「离散指令」升级为「本地模型驱动的参数级控制」：情绪提取（规则兜底 + ONNX 预留）、动作调度、参数映射、口型/眨眼/呼吸时序协调；呼吸禁用保留眨眼/头发/身体轻微起伏。

**Architecture:** 在 `live2d_orchestrator/` 新增 5 个子模块：ParameterRegistry（解析 model3.json）、EmotionExtractor（规则+ONNX 预留）、MotionScheduler、ParameterMapper、TimingController。Live2DOrchestrator 新增 `live2d:emotion` 与 `live2d:params_update` 能力；高频参数走 `live2d:params_batch`（10Hz 聚合帧），前端 30fps 插值渲染。TTS 音素时间戳接口预留。

**Tech Stack:** Python 3.10 / json / threading / EventBus / pytest / 前端 vanilla JS（live2d_actor.js）

**参考规格：** `docs/superpowers/specs/2026-08-22-five-phase-upgrade-design.md` 第 3 章（QA 决策 Q5-Q7）

---

### Task 1: 事件注册（events.py）

**Files:**
- Modify: `src/shared/events.py`

- [ ] **Step 1: 增加事件常量（放在 Live2D 域后）**

```python
# ========== Live2D 参数驱动域（任务三） ==========
EMOTION_EXTRACTED = "emotion:extracted"               # 情绪提取完成（emotion/score/role）
LIVE2D_PARAMS_BATCH = "live2d:params_batch"           # 批量参数帧（10Hz 聚合，role/params/ts）
```

- [ ] **Step 2: 收录 ALL_EVENTS**

在 `ALL_EVENTS` 内追加 `EMOTION_EXTRACTED, LIVE2D_PARAMS_BATCH,`。

- [ ] **Step 3: 运行 schema 测试**

Run: `python -m pytest tests/test_events_schema.py -q`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add src/shared/events.py
git commit -m "feat(events): 情绪/参数帧事件（emotion:extracted / live2d:params_batch）"
```

---

### Task 2: ParameterRegistry（解析 model3.json 参数清单）

**Files:**
- Create: `src/orchestrators/live2d_orchestrator/parameter_registry.py`
- Test: `tests/test_parameter_registry.py`

- [ ] **Step 1: 写失败测试**

```python
"""test_parameter_registry.py — Live2D 参数注册表"""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.live2d_orchestrator.parameter_registry import ParameterRegistry


def _write_model(tmp_path, name="Haru"):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.model3.json").write_text(json.dumps({
        "Version": 3, "FileReferences": {},
        "Parameters": [
            {"Id": "ParamAngleX", "Type": "Float", "Min": -30.0, "Max": 30.0, "Default": 0.0},
            {"Id": "ParamEyeLOpen", "Type": "Float", "Min": 0.0, "Max": 1.0, "Default": 1.0},
            {"Id": "ParamMouthOpenY", "Type": "Float", "Min": 0.0, "Max": 1.0, "Default": 0.0},
        ]}, ensure_ascii=False))
    return d


def test_load_parses_parameters(tmp_path):
    d = _write_model(tmp_path)
    reg = ParameterRegistry(models_dir=str(d))
    params = reg.load("Haru")
    assert "ParamAngleX" in params
    assert params["ParamAngleX"] == {"min": -30.0, "max": 30.0, "default": 0.0}
    assert params["ParamMouthOpenY"]["max"] == 1.0


def test_load_cached(tmp_path):
    d = _write_model(tmp_path)
    reg = ParameterRegistry(models_dir=str(d))
    assert reg.load("Haru") is reg.load("Haru")  # 缓存同一实例


def test_missing_model_returns_empty(tmp_path):
    reg = ParameterRegistry(models_dir=str(tmp_path))
    assert reg.load("NoSuchModel") == {}


def test_get_returns_none_for_unknown(tmp_path):
    d = _write_model(tmp_path)
    reg = ParameterRegistry(models_dir=str(d))
    reg.load("Haru")
    assert reg.get("Haru", "ParamNope") is None
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_parameter_registry.py -q`
Expected: FAIL

- [ ] **Step 3: 实现 parameter_registry.py**

```python
"""parameter_registry.py — Live2D 参数注册表（任务三）

模型加载时解析 data/models/<name>/<name>.model3.json 的 Parameters 段
（Id/Type/Min/Max/Default）并缓存；供 ParameterMapper 映射前校验参数存在性与范围。

# 模块内容清单 — parameter_registry
## 1. 模块身份标识
- 所属调度官：live2d · parameter_registry · 能力 live2d:params_update 的参数来源
## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| models_dir | 否 | <项目根>/data/models | str | Live2D 模型目录 |
## 3. 输入契约
- load(model_name) -> Dict[str, Dict]；get(model_name, param_id) -> Optional[Dict]
## 4. 输出契约
- 成功：{param_id: {"min", "max", "default"}}；get 命中返回参数定义
- 失败：模型/文件缺失返回 {} / None（不抛异常）
## 5. 依赖声明
- 外部服务：无（解析本地 .model3.json）
- 内部模块：json、pathlib、typing、shared.config_loader（PROJECT_ROOT）
## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 文件缺失/解析失败 | model3.json 不存在或 JSON 损坏 | load 返回 {} 并记录警告 |
## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | - | 构造即用，_cache 懒加载 |
## 8. 领域状态说明
- 状态项：_cache（model_name -> 参数表）、_models_dir
- 持久化：无（每次构造重新解析）
"""
import json
import logging
from pathlib import Path
from typing import Dict, Optional

from src.shared.config_loader import PROJECT_ROOT

logger = logging.getLogger(__name__)

MODELS_DIR = PROJECT_ROOT / "data" / "models"


class ParameterRegistry:
    """解析并缓存 Live2D 模型参数定义。"""

    def __init__(self, models_dir: str = ""):
        self._models_dir = Path(models_dir) if models_dir else MODELS_DIR
        self._cache: Dict[str, Dict[str, Dict[str, float]]] = {}

    def load(self, model_name: str) -> Dict[str, Dict[str, float]]:
        """解析模型参数表并缓存；文件缺失返回空表。"""
        if model_name in self._cache:
            return self._cache[model_name]
        model_path = self._find_model3(model_name)
        if model_path is None:
            logger.warning("[ParameterRegistry] 未找到模型 %s 的 model3.json", model_name)
            self._cache[model_name] = {}
            return self._cache[model_name]
        params: Dict[str, Dict[str, float]] = {}
        try:
            data = json.loads(model_path.read_text(encoding="utf-8"))
            for p in data.get("Parameters", []) or []:
                pid = p.get("Id")
                if not pid:
                    continue
                params[pid] = {
                    "min": float(p.get("Min", 0.0)),
                    "max": float(p.get("Max", 1.0)),
                    "default": float(p.get("Default", 0.0)),
                }
            self._cache[model_name] = params
            logger.info("[ParameterRegistry] %s 参数加载: %d 个", model_name, len(params))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("[ParameterRegistry] %s 解析失败: %s", model_name, e)
            self._cache[model_name] = {}
        return self._cache[model_name]

    def get(self, model_name: str, param_id: str) -> Optional[Dict[str, float]]:
        params = self.load(model_name)
        return params.get(param_id)

    def _find_model3(self, model_name: str) -> Optional[Path]:
        for cand in (self._models_dir / model_name / f"{model_name}.model3.json",
                     self._models_dir / f"{model_name}.model3.json",
                     self._models_dir / f"{model_name}.json"):
            if cand.exists():
                return cand
        return None
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_parameter_registry.py -q`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add src/orchestrators/live2d_orchestrator/parameter_registry.py tests/test_parameter_registry.py
git commit -m "feat(live2d): ParameterRegistry 解析 model3.json 参数清单"
```

---

### Task 3: EmotionExtractor（规则兜底 + ONNX 预留）

**Files:**
- Create: `src/orchestrators/live2d_orchestrator/emotion_extractor.py`
- Test: `tests/test_emotion_extractor.py`

- [ ] **Step 1: 写失败测试**

```python
"""test_emotion_extractor.py — 情绪提取（规则兜底）"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.live2d_orchestrator.emotion_extractor import EmotionExtractor


def _e(text):
    return EmotionExtractor().extract(text)


def test_happy_word():
    r = _e("今天好开心啊！")
    assert r["emotion"] == "开心"
    assert r["source"] == "rule"


def test_angry_word_and_punct():
    r = _e("哼！真是气死我了")
    assert r["emotion"] == "生气"


def test_surprised_punct():
    r = _e("什么？！竟然是这样")
    assert r["emotion"] == "惊讶"


def test_calm_default():
    r = _e("嗯，好的")
    assert r["emotion"] == "平静"


def test_score_in_range():
    r = _e("超级开心！！！")
    assert 0.0 <= r["score"] <= 1.0


def test_empty_text_calm():
    r = _e("")
    assert r["emotion"] == "平静"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_emotion_extractor.py -q`
Expected: FAIL

- [ ] **Step 3: 实现 emotion_extractor.py**

```python
"""emotion_extractor.py — 情绪提取（任务三）

本地轻量模型驱动；模型未就绪（P1）用规则兜底：情绪词典命中计数 + 标点/语气词加成。
ONNX 接口预留：模型文件放入 data/models/emotion/ 时自动启用（_onnx_available 探测）。

# 模块内容清单 — emotion_extractor
## 1. 模块身份标识
- 所属调度官：live2d · emotion_extractor · 能力 live2d:emotion 的情绪来源
## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| 无 | - | - | - | 规则词典为模块常量 |
## 3. 输入契约
- extract(text: str) -> {"emotion", "score", "source"}
## 4. 输出契约
- 成功：emotion ∈ {开心,难过,惊讶,害羞,生气,平静}；score ∈ [0,1]；source ∈ rule/onnx
- 失败：空文本 → 平静 0.0
## 5. 依赖声明
- 外部服务：无（ONNX 可选，缺失自动降级规则）
- 内部模块：无（纯 Python）
## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| ONNX 推理异常 | 模型存在但推理失败 | 捕获并回退规则结果 |
## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | - | 无状态 |
## 8. 领域状态说明
- 状态项：无
- 持久化：无
"""
import logging
from typing import Dict

logger = logging.getLogger(__name__)

VALID_EMOTIONS = ("开心", "难过", "惊讶", "害羞", "生气", "平静")

# 规则词典（P1 兜底）：词 → 情绪（命中计数）
EMOTION_WORDS = {
    "开心": ["开心", "高兴", "哈哈", "太好啦", "喜欢", "棒", "耶", "嘻嘻", "嘿嘿", "好耶"],
    "难过": ["难过", "伤心", "呜呜", "哭了", "委屈", "唉", "遗憾", "心痛", "想哭"],
    "惊讶": ["惊讶", "震惊", "哇", "天哪", "居然", "竟然", "没想到", "怎么可能"],
    "害羞": ["害羞", "不好意思", "脸红", "羞涩", "难为情", "害羞了"],
    "生气": ["生气", "气死", "讨厌", "哼", "烦", "可恶", "怒了", "不满"],
}
# 标点加成：标点 → 情绪（单次命中强权重）
EMOTION_PUNCT = {"！": "惊讶", "!": "惊讶", "？": "惊讶", "…": "难过", "...": "难过", "呜呜": "难过"}
# 语气词弱信号（无词命中时做 tie-break）
TONE_WORDS = {"哼": "生气", "呀": "开心", "嘛": "平静", "呢": "平静", "啊": "惊讶"}


class EmotionExtractor:
    """文本 → 情绪标签（规则兜底 + ONNX 预留）。"""

    def extract(self, text: str) -> Dict[str, str]:
        text = (text or "").strip()
        if not text:
            return {"emotion": "平静", "score": 0.0, "source": "rule"}
        onnx_result = self._onnx_extract(text)
        if onnx_result is not None:
            return onnx_result
        return self._rule_extract(text)

    # ---------- 规则兜底 ----------

    def _rule_extract(self, text: str) -> Dict[str, str]:
        scores = {e: 0 for e in VALID_EMOTIONS}
        for emotion, words in EMOTION_WORDS.items():
            for w in words:
                if w in text:
                    scores[emotion] += 1
        for ch, emotion in EMOTION_PUNCT.items():
            if ch in text:
                scores[emotion] = scores.get(emotion, 0) + 1
        best, best_score = "平静", 0
        for emotion, s in scores.items():
            if s > best_score:
                best, best_score = emotion, s
        if best_score == 0:
            for ch, emotion in TONE_WORDS.items():
                if ch in text:
                    return {"emotion": emotion, "score": 0.3, "source": "rule"}
            return {"emotion": "平静", "score": 0.1, "source": "rule"}
        score = min(1.0, 0.4 + 0.2 * best_score)
        return {"emotion": best, "score": round(score, 2), "source": "rule"}

    # ---------- ONNX 预留（P2：模型文件就绪后启用） ----------

    def _onnx_extract(self, text: str):
        try:
            import importlib.util
            if importlib.util.find_spec("onnxruntime") is None:
                return None
            from pathlib import Path
            from src.shared.config_loader import PROJECT_ROOT
            model_path = PROJECT_ROOT / "data" / "models" / "emotion" / "emotion.onnx"
            if not model_path.exists():
                return None
            # TODO(P2): 加载 onnxruntime 会话并推理（标签映射到 VALID_EMOTIONS）
            return None  # 模型推理待模型文件落地后实现
        except Exception as e:
            logger.debug("[EmotionExtractor] ONNX 不可用，规则兜底: %s", e)
            return None
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_emotion_extractor.py -q`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add src/orchestrators/live2d_orchestrator/emotion_extractor.py tests/test_emotion_extractor.py
git commit -m "feat(live2d): EmotionExtractor 规则兜底 + ONNX 预留"
```

---

### Task 4: ParameterMapper（情绪+动作 → 参数范围）

**Files:**
- Create: `src/orchestrators/live2d_orchestrator/parameter_mapper.py`
- Test: `tests/test_parameter_mapper.py`

- [ ] **Step 1: 写失败测试**

```python
"""test_parameter_mapper.py — 情绪/动作 → Live2D 参数映射"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.live2d_orchestrator.parameter_mapper import ParameterMapper


class FakeRegistry:
    def get(self, model, pid):
        known = {"ParamSmile": {"min": -1.0, "max": 1.0},
                 "ParamEyeLOpen": {"min": 0.0, "max": 1.0},
                 "ParamAngleZ": {"min": -30.0, "max": 30.0}}
        return known.get(pid)


def _mapper():
    return ParameterMapper(registry=FakeRegistry())


def test_happy_maps_smile():
    m = _mapper()
    params = m.map("开心", model="Haru")
    assert "ParamSmile" in params
    assert 0.0 <= params["ParamSmile"] <= 1.0


def test_unknown_emotion_calm():
    m = _mapper()
    params = m.map("不存在", model="Haru")
    assert params == {}


def test_clamps_to_registry_range():
    m = _mapper()
    params = m.map("惊讶", model="Haru")
    assert "ParamEyeLOpen" in params
    assert 0.0 <= params["ParamEyeLOpen"] <= 1.0
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_parameter_mapper.py -q`
Expected: FAIL

- [ ] **Step 3: 实现 parameter_mapper.py**

```python
"""parameter_mapper.py — 情绪/动作 → Live2D 参数值映射（任务三）

将情绪标签 + 动作映射为具体参数值（范围取中值并钳制到注册表范围）。
映射前检查参数在模型注册表中存在，未知参数跳过（跨模型兼容）。

# 模块内容清单 — parameter_mapper
## 1. 模块身份标识
- 所属调度官：live2d · parameter_mapper · 能力 live2d:emotion/params_update 的参数来源
## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| 无 | - | - | - | 映射表为模块常量，registry 注入 |
## 3. 输入契约
- map(emotion, motion="idle", model="") -> Dict[str, float]
## 4. 输出契约
- 成功：{param_id: value}（值已钳制到注册表范围）；未知情绪/无映射 → {}
## 5. 依赖声明
- 外部服务：无
- 内部模块：parameter_registry（注入，检查参数存在性）
## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 无 | - | 参数不存在于模型 → 跳过 |
## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | - | 无状态 |
## 8. 领域状态说明
- 状态项：无
- 持久化：无
"""
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# 情绪 → 参数目标值（映射值取 0..1 归一，实际按注册表范围钳制）
EMOTION_PARAMS: Dict[str, Dict[str, float]] = {
    "开心": {"ParamSmile": 0.8, "ParamMouthSmile": 0.8, "ParamEyeLOpen": 0.7},
    "难过": {"ParamAngleZ": 0.25, "ParamMouthForm": 0.3, "ParamEyeLOpen": 0.35, "ParamEyeROpen": 0.35},
    "惊讶": {"ParamEyeLOpen": 0.95, "ParamEyeROpen": 0.95, "ParamMouthOpenY": 0.6, "ParamMouthForm": 0.6},
    "害羞": {"ParamAngleX": 0.15, "ParamEyeLOpen": 0.3, "ParamMouthSmile": 0.4},
    "生气": {"ParamAngleZ": 0.3, "ParamMouthForm": 0.7, "ParamEyeLOpen": 0.85, "ParamEyeROpen": 0.85},
    "平静": {},
}
# 动作 → 参数目标值（幅度归一）
MOTION_PARAMS: Dict[str, Dict[str, float]] = {
    "wave": {"ParamAngleZ": 0.35},
    "nod": {"ParamAngleX": 0.25},
    "shake": {"ParamAngleZ": 0.2},
    "idle": {},
}


class ParameterMapper:
    """情绪 + 动作 → 具体参数值（钳制到模型注册表范围）。"""

    def __init__(self, registry=None):
        self._registry = registry

    def map(self, emotion: str, motion: str = "idle", model: str = "") -> Dict[str, float]:
        merged: Dict[str, float] = {}
        for source in (EMOTION_PARAMS.get(emotion or "平静", {}),
                       MOTION_PARAMS.get(motion or "idle", {})):
            for pid, target in source.items():
                value = self._resolve(pid, target, model)
                if value is not None:
                    merged[pid] = value
        return merged

    def _resolve(self, pid: str, target: float, model: str) -> Optional[float]:
        if self._registry is None:
            return round(target, 3)
        spec = self._registry.get(model, pid)
        if spec is None:
            return None  # 模型无此参数，跳过
        lo, hi = float(spec.get("min", 0.0)), float(spec.get("max", 1.0))
        value = lo + (hi - lo) * max(0.0, min(1.0, target))
        return round(value, 3)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_parameter_mapper.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add src/orchestrators/live2d_orchestrator/parameter_mapper.py tests/test_parameter_mapper.py
git commit -m "feat(live2d): ParameterMapper 情绪/动作 → 参数值映射"
```

---

### Task 5: TimingController（时序协调：口型/眨眼/起伏）

**Files:**
- Create: `src/orchestrators/live2d_orchestrator/timing_controller.py`
- Test: `tests/test_timing_controller.py`

- [ ] **Step 1: 写失败测试**

```python
"""test_timing_controller.py — 时序协调（口型/眨眼/身体起伏）"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.live2d_orchestrator.timing_controller import TimingController


def test_blink_cycle_changes_eye_open():
    t = TimingController(blink_interval=(1.0, 1.0))  # 固定 1s 便于测试
    # 非说话时段 tick：眼开度随时间变化（睁→闭→睁）
    values = {t.tick(now=i, speaking=False).get("ParamEyeLOpen", 1.0)
              for i in range(0, 12, 2)}
    assert len(values) > 1  # 有开合变化


def test_speaking_suppresses_motion_switch():
    t = TimingController()
    assert t.should_switch_motion(now=0.0, speaking=True) is False
    assert t.should_switch_motion(now=0.0, speaking=False) is True


def test_idle_body_breathing_enabled():
    t = TimingController()
    params = t.tick(now=0.0, speaking=False)
    # 呼吸功能禁用：不应出现大幅身体参数；轻微起伏保留
    body = params.get("ParamBodyAngleZ", 0.0)
    assert abs(body) <= 0.15
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_timing_controller.py -q`
Expected: FAIL

- [ ] **Step 3: 实现 timing_controller.py**

```python
"""timing_controller.py — 时序协调（任务三）

口型优先：说话期间抑制动作切换；非说话时段输出「身体轻微起伏（正弦）+ 周期性眨眼」，
呼吸功能禁用（不产生大幅呼吸参数）。供 orchestrator 周期性 tick 调用。

# 模块内容清单 — timing_controller
## 1. 模块身份标识
- 所属调度官：live2d · timing_controller · 能力 live2d:params_update 的 idle 时序来源
## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| blink_interval | 否 | (3,5) | (float,float) | 眨眼周期秒范围 |
| body_amplitude | 否 | 0.08 | float | 身体起伏幅度（归一 0..1） |
## 3. 输入契约
- tick(now, speaking=False, lip_end_at=0.0) -> Dict[str, float]
- should_switch_motion(now, speaking) -> bool
## 4. 输出契约
- 成功：参数增量 dict（ParamEyeLOpen/ParamBodyAngleZ 等）；should_switch_motion 布尔
- 失败：无异常路径
## 5. 依赖声明
- 外部服务：无
- 内部模块：math、typing
## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 无 | - | - |
## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | - | 无状态（纯函数式） |
## 8. 领域状态说明
- 状态项：无
- 持久化：无
"""
import math
import random
import time
from typing import Dict


class TimingController:
    """口型/眨眼/身体起伏时序协调（呼吸禁用）。"""

    def __init__(self, blink_interval=(3.0, 5.0), body_amplitude: float = 0.08):
        self._blink_interval = blink_interval
        self._body_amp = body_amplitude
        self._phase_offset = random.uniform(0.0, math.tau)

    def tick(self, now: float = None, speaking: bool = False,
             lip_end_at: float = 0.0) -> Dict[str, float]:
        """非说话时段：身体正弦起伏 + 周期性眨眼。返回参数增量。"""
        now = now if now is not None else time.time()
        params: Dict[str, float] = {}
        if not speaking:
            # 身体轻微起伏（呼吸禁用后的最小"活着"感）
            body = self._body_amp * math.sin(now * 0.6 + self._phase_offset)
            params["ParamBodyAngleZ"] = round(body, 4)
            # 周期性眨眼：眼开度 1 → 0 → 1（短促）
            lo, hi = self._blink_interval
            period = max(lo, min(hi, random.uniform(lo, hi)))
            phase = (now % period) / period
            if phase < 0.08:
                params["ParamEyeLOpen"] = round(max(0.0, 1.0 - phase / 0.08), 3)
                params["ParamEyeROpen"] = round(max(0.0, 1.0 - phase / 0.08), 3)
        return params

    @staticmethod
    def should_switch_motion(now: float, speaking: bool) -> bool:
        """说话期间不切换动作（口型优先）。"""
        return not speaking
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_timing_controller.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add src/orchestrators/live2d_orchestrator/timing_controller.py tests/test_timing_controller.py
git commit -m "feat(live2d): TimingController 时序协调（口型优先/眨眼/身体起伏）"
```

---

### Task 6: Live2DOrchestrator 新能力（emotion / params_update）

**Files:**
- Modify: `src/orchestrators/live2d_orchestrator/live2d_orchestrator.py`
- Modify: `src/orchestrators/live2d_orchestrator/registry.py`
- Test: `tests/test_live2d_orchestrator.py`

- [ ] **Step 1: 更新 registry.py 能力表**

```python
CAPABILITIES = {
    "live2d:load": [object],
    "live2d:expression": [object],
    "live2d:motion": [object],
    "live2d:lip_sync": [object],
    "live2d:emotion": [object],        # 文本 → 情绪 + 参数（任务三）
    "live2d:params_update": [object],  # 批量参数帧（任务三）
}
```

- [ ] **Step 2: 新增 orchestrator 能力测试**

```python
def test_emotion_capability_updates_params():
    """live2d:emotion：文本 → 情绪提取 + 参数映射，发布 emotion:extracted。"""
    import asyncio
    from src.shared.events import EMOTION_EXTRACTED
    bus = EventBus()
    bus.reset()
    orch = Live2DOrchestrator(event_bus=bus)
    asyncio.run(orch.handle({"capability": "live2d:load",
                             "payload": {"model_name": "Haru", "role": "yuki"}}))
    seen = {}
    bus.subscribe(EMOTION_EXTRACTED, lambda event, **kw: seen.update(kw))
    r = asyncio.run(orch.handle({"capability": "live2d:emotion",
                                 "payload": {"text": "今天好开心啊！", "role": "yuki"}}))
    assert r["ok"] is True
    assert seen.get("emotion") == "开心"
    assert seen.get("role") == "yuki"


def test_params_update_publishes_batch():
    """live2d:params_update：批量参数帧 → 发布 live2d:params_batch。"""
    import asyncio
    from src.shared.events import LIVE2D_PARAMS_BATCH
    bus = EventBus()
    bus.reset()
    orch = Live2DOrchestrator(event_bus=bus)
    asyncio.run(orch.handle({"capability": "live2d:load",
                             "payload": {"model_name": "Haru", "role": "yuki"}}))
    seen = {}
    bus.subscribe(LIVE2D_PARAMS_BATCH, lambda event, **kw: seen.update(kw))
    r = asyncio.run(orch.handle({"capability": "live2d:params_update",
                                 "payload": {"role": "yuki",
                                             "params": {"ParamEyeLOpen": 0.5}, "ts": 1.0}}))
    assert r["ok"] is True
    assert seen.get("params") == {"ParamEyeLOpen": 0.5}
```

- [ ] **Step 3: 修改 live2d_orchestrator.py**

`__init__` 装配新子模块，并新增两个能力方法：

```python
    def __init__(self, event_bus):
        self._event_bus = event_bus
        self._models: Dict[str, Dict[str, Any]] = {}
        self._lip_threads: Dict[str, threading.Thread] = {}
        self._started = False
        # 任务三：本地模型驱动子模块
        from src.orchestrators.live2d_orchestrator.parameter_registry import ParameterRegistry
        from src.orchestrators.live2d_orchestrator.emotion_extractor import EmotionExtractor
        from src.orchestrators.live2d_orchestrator.parameter_mapper import ParameterMapper
        from src.orchestrators.live2d_orchestrator.timing_controller import TimingController
        self._registry = ParameterRegistry()
        self._emotion = EmotionExtractor()
        self._mapper = ParameterMapper(registry=self._registry)
        self._timing = TimingController()
        registry.bind(self.handle)
```

`handle` 增加：

```python
        if capability == "live2d:emotion":
            return self._emotion_change(payload)
        if capability == "live2d:params_update":
            return self._params_update(payload)
```

新增方法：

```python
    def _emotion_change(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """文本 → 情绪提取 + 参数映射 + 事件发布（任务三）。"""
        role = payload.get("role", DEFAULT_ROLE)
        st = self._state(role)
        if st["model"] is None:
            return {"ok": False, "data": {}, "error": "模型未加载，请先 live2d:load"}
        result = self._emotion.extract(payload.get("text", ""))
        emotion = result["emotion"]
        params = self._mapper.map(emotion, model=st["model"])
        st["emotion"] = emotion
        st["params"] = params
        self._event_bus.publish(EMOTION_EXTRACTED, emotion=emotion,
                                score=result["score"], role=role, params=params)
        self._push_status()
        return {"ok": True, "data": {"emotion": emotion, "params": params}, "error": None}

    def _params_update(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """批量参数帧（10Hz 聚合）：更新状态 + 发布 live2d:params_batch。"""
        role = payload.get("role", DEFAULT_ROLE)
        st = self._state(role)
        params = payload.get("params", {}) or {}
        st.setdefault("params", {}).update(params)
        self._event_bus.publish(LIVE2D_PARAMS_BATCH, role=role,
                                params=dict(params), ts=payload.get("ts", 0.0))
        return {"ok": True, "data": {"applied": len(params)}, "error": None}
```

`imports` 追加 `EMOTION_EXTRACTED, LIVE2D_PARAMS_BATCH`（从 shared.events）。

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/test_live2d_orchestrator.py tests/test_placeholder_modules.py -q`
Expected: PASS（含新增 2 条）

- [ ] **Step 5: 提交**

```bash
git add src/orchestrators/live2d_orchestrator/ tests/test_live2d_orchestrator.py
git commit -m "feat(live2d): live2d:emotion / live2d:params_update 能力"
```

---

### Task 7: 前端参数帧消费 + 30fps 插值

**Files:**
- Modify: `frontend/live2d_stream/live2d_actor.js`

- [ ] **Step 1: 查看现有结构**

Read: `frontend/live2d_stream/live2d_actor.js`（确认参数处理入口与渲染循环）

- [ ] **Step 2: 新增参数帧消费（在 WS 事件处理处追加）**

```javascript
// —— 任务三：live2d:params_batch 参数帧消费 + 30fps 插值 ——
let _targetParams = {};      // 目标参数（最近一帧）
let _interpParams = {};      // 插值中的参数
let _paramLerp = 1.0;        // 0..1 插值进度

// WS 收到 live2d:params_batch 时调用
function onParamsBatch(payload) {
  if (payload && payload.params) {
    _targetParams = Object.assign({}, payload.params);
    _paramLerp = 0.0; // 触发插值
  }
}

// 渲染循环内（约 30fps）调用：把插值参数应用到模型
function applyInterpolatedParams(dt) {
  if (_paramLerp >= 1.0) return;
  _paramLerp = Math.min(1.0, _paramLerp + dt * 10.0); // 10Hz 帧 → 100ms 内插完
  for (const pid in _targetParams) {
    const target = _targetParams[pid];
    const prev = _interpParams[pid] !== undefined ? _interpParams[pid] : target;
    const value = prev + (target - prev) * _paramLerp;
    _interpParams[pid] = value;
    // 应用到 Live2D 模型（PixiJS）：model.internalModel.coreModel.setParameterValueById
    if (window.live2dModel && live2dModel.internalModel) {
      live2dModel.internalModel.coreModel.setParameterValueById(pid, value);
    }
  }
  if (_paramLerp >= 1.0) { _interpParams = Object.assign({}, _targetParams); }
}
```

- [ ] **Step 3: 接线**

在 WS 事件分发处：事件名为 `live2d:params_batch` 时调用 `onParamsBatch(payload)`；
在渲染循环（`app.ticker` 或 rAF）中调用 `applyInterpolatedParams(dt)`。

- [ ] **Step 4: 冒烟验证（浏览器手工）**

启动服务 → 打开 `/live2d/` → 后端调 `live2d:emotion`（文本「今天好开心啊！」）→ 观察表情参数变化。
Expected: 前端模型参数平滑变化（无跳变）

- [ ] **Step 5: 提交**

```bash
git add frontend/live2d_stream/live2d_actor.js
git commit -m "feat(live2d): 前端参数帧消费 + 30fps 插值"
```

---

### Task 8: 全量验证

- [ ] **Step 1: 全量 pytest**

Run: `python -m pytest tests -q`
Expected: 全绿

- [ ] **Step 2: 冒烟 L0**

Run: `python scripts/smoke_test.py --check-env`
Expected: 全 PASS

- [ ] **Step 3: 提交（若有遗漏变更）**

```bash
git add -A
git commit -m "test(live2d): 任务三全量验证"
```

**任务三出口条件：** 5 子模块 + 2 新能力 + 事件 + 前端插值落地；新增测试全绿；现有测试不回归。
