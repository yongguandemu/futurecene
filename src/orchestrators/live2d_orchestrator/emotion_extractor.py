"""emotion_extractor.py — 情绪提取（本地模型 + 规则兜底）

文本 → 情绪标签，三档来源（live2d.emotion.source）：
- rule：中文规则词典（零依赖、零延迟，直播开箱即用）
- model：本地 ONNX 情绪模型（data/models/emotion/，CPU 推理毫秒级）
- auto（默认）：模型就绪用模型，否则自动回落规则

命中率训练（文本→情绪→参数的对应关系调优）：
- evaluate(dataset) 统计规则/模型命中率与混淆矩阵
- 外部词典 data/models/emotion/lexicon.json 可扩展规则词库（训练脚本产出）
- 外部标签映射 data/models/emotion/labels_map.json 可调整模型标签→内部情绪（6 类）

# 模块内容清单 — emotion_extractor

## 1. 模块身份标识
- 所属调度官：live2d · emotion_extractor · 能力 live2d:emotion 的情绪来源

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| live2d.emotion.source | 否 | auto | rule/model/auto | 情绪提取来源；model 缺模型时降级 rule |
| live2d.emotion.confidence_threshold | 否 | 0.0 | [0,1] | 模型置信度低于阈值回落规则；0=禁用 |
| data/models/emotion/model.onnx | 否 | - | 文件 | ONNX 模型文件（缺则 model/auto 降级 rule） |
| data/models/emotion/tokenizer.json | 否 | - | 文件 | tokenizers 分词文件 |
| data/models/emotion/config.json | 否 | - | 文件 | id2label 标签表 |
| data/models/emotion/lexicon.json | 否 | - | 文件 | 规则词典扩展（训练脚本产出） |
| data/models/emotion/labels_map.json | 否 | - | 文件 | 模型标签→内部情绪映射覆盖 |

## 3. 输入契约
- extract(text: str) -> {"emotion", "score", "source"}
- evaluate(dataset: list[(text, expected_emotion)]) -> stats

## 4. 输出契约
- 成功：emotion ∈ VALID_EMOTIONS；score ∈ [0,1]；source ∈ rule/onnx
- 失败：空文本 → 平静 0.0；模型推理异常 → 回落规则

## 5. 依赖声明
- 外部服务：无（onnxruntime + tokenizers 可选，缺失自动降级规则）
- 内部模块：src.shared.config_loader（仅读取 PROJECT_ROOT）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| ONNX 推理异常 | 模型存在但推理失败 | 捕获并回退规则结果 |
| 模型文件缺失 | source=model 但文件未就绪 | 降级 rule 并告警 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | - | 构造时惰性加载模型会话 |

## 8. 领域状态说明
- 状态项：_session/_tokenizer/_model_labels（ONNX 会话）
- 持久化：无（会话随进程，模型文件在 data/models/emotion/）
- 恢复：重启后首次 extract 重新探测加载
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

VALID_EMOTIONS = ("开心", "难过", "惊讶", "害羞", "生气", "平静")

# 规则词典（兜底）：词 → 情绪（命中计数）
EMOTION_WORDS = {
    "开心": ["开心", "高兴", "哈哈", "太好啦", "喜欢", "棒", "耶", "嘻嘻", "嘿嘿", "好耶"],
    "难过": ["难过", "伤心", "呜呜", "哭了", "委屈", "唉", "遗憾", "心痛", "想哭"],
    "惊讶": ["惊讶", "震惊", "哇", "天哪", "居然", "竟然", "没想到", "怎么可能"],
    "害羞": ["害羞", "不好意思", "脸红", "羞涩", "难为情", "害羞了"],
    "生气": ["生气", "气死", "讨厌", "哼", "烦", "可恶", "怒了", "不满"],
}
# 标点加成：标点 → 情绪（单次命中强权重）
EMOTION_PUNCT = {"！": "惊讶", "!": "惊讶", "？": "惊讶", "…": "难过", "...": "难过"}
# 语气词弱信号（无词命中时做 tie-break）
TONE_WORDS = {"哼": "生气", "呀": "开心", "嘛": "平静", "呢": "平静", "啊": "惊讶"}

# 模型标签（8 分类）→ 内部情绪（6 类）默认映射；可由 labels_map.json 覆盖
MODEL_LABEL_MAP = {
    "开心": "开心",
    "难过": "难过",
    "生气": "生气",
    "惊讶": "惊讶",
    "害怕": "惊讶",    # 恐惧 → 瞪眼张嘴表情
    "厌恶": "生气",    # 厌恶 → 生气表情
    "中性": "平静",
    "关心": "开心",    # 关切/温暖 → 正面表情
}

EMOTION_DIR = Path(__file__).resolve().parents[3] / "data" / "models" / "emotion"


class EmotionExtractor:
    """文本 → 情绪标签（rule / model / auto 三档）。"""

    def __init__(self, source: str = "auto", model_dir: Optional[Path] = None,
                 lexicon_path: Optional[Path] = None,
                 labels_map_path: Optional[Path] = None,
                 confidence_threshold: float = 0.0) -> None:
        self._source = source if source in ("rule", "model", "auto") else "auto"
        self._model_dir = Path(model_dir) if model_dir else EMOTION_DIR
        self._threshold = max(0.0, min(1.0, float(confidence_threshold)))
        # 外部词典/映射（训练落点）：lexicon.json 扩展词库，labels_map.json 覆盖模型标签映射
        self._lexicon = self._load_lexicon(lexicon_path or self._model_dir / "lexicon.json")
        self._labels_map = self._load_labels_map(labels_map_path or self._model_dir / "labels_map.json")
        self._session = None      # onnxruntime InferenceSession（惰性加载）
        self._tokenizer = None    # tokenizers.Tokenizer
        self._model_labels: List[str] = []
        self._onnx_ready = False  # 首次 extract 时探测（避免构造即加载大模型）

    # ---------- 对外接口 ----------

    def extract(self, text: str) -> Dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"emotion": "平静", "score": 0.0, "source": "rule"}
        # 惰性初始化：首次调用才加载模型（避免构造即加载 600MB+）
        if self._session is None and self._source in ("model", "auto"):
            self._init_onnx()
        use_model = self._onnx_ready and self._source != "rule"
        if use_model:
            result = self._onnx_extract(text)
            if result is not None:
                return result
        return self._rule_extract(text)

    def evaluate(self, dataset: List[Tuple[str, str]]) -> Dict[str, Any]:
        """命中率评测：rule 与 onnx 分列统计（数据集 [(text, expected_emotion)]）。

        返回 {total, rule:{hit,accuracy,per_emotion}, onnx:{...}, samples: [...错误样本]}
        """
        if self._session is None and self._source in ("model", "auto"):
            self._init_onnx()  # 评测前确保模型就绪探测
        stats = {"total": len(dataset), "rule": self._stats(dataset, "rule"),
                 "onnx": self._stats(dataset, "onnx"), "samples": []}
        for text, expected in dataset:
            r = self._rule_extract(text)
            o = self._onnx_extract(text) if self._onnx_ready else None
            if r["emotion"] != expected or (o is not None and o["emotion"] != expected):
                stats["samples"].append({"text": text, "expected": expected,
                                         "rule": r["emotion"], "onnx": o["emotion"] if o else None})
        return stats

    # ---------- 规则兜底 ----------

    def _rule_extract(self, text: str) -> Dict[str, Any]:
        scores = {e: 0 for e in VALID_EMOTIONS}
        for emotion, words in self._lexicon["words"].items():
            for w in words:
                if w in text:
                    scores[emotion] += 1
        for ch, emotion in self._lexicon["punct"].items():
            if ch in text:
                scores[emotion] = scores.get(emotion, 0) + 1
        best, best_score = "平静", 0
        for emotion, s in scores.items():
            if s > best_score:
                best, best_score = emotion, s
        if best_score == 0:
            for ch, emotion in self._lexicon["tone"].items():
                if ch in text:
                    return {"emotion": emotion, "score": 0.3, "source": "rule"}
            return {"emotion": "平静", "score": 0.1, "source": "rule"}
        score = min(1.0, 0.4 + 0.2 * best_score)
        return {"emotion": best, "score": round(score, 2), "source": "rule"}

    # ---------- ONNX 模型 ----------

    def _init_onnx(self) -> bool:
        try:
            import importlib.util
            if importlib.util.find_spec("onnxruntime") is None:
                return False
            if importlib.util.find_spec("tokenizers") is None:
                return False
            model_path = self._model_dir / "model.onnx"
            tok_path = self._model_dir / "tokenizer.json"
            cfg_path = self._model_dir / "config.json"
            if not (model_path.exists() and tok_path.exists() and cfg_path.exists()):
                return False
            import onnxruntime as ort
            from tokenizers import Tokenizer
            self._session = ort.InferenceSession(str(model_path),
                                                 providers=["CPUExecutionProvider"])
            self._tokenizer = Tokenizer.from_file(str(tok_path))
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            id2label = cfg.get("id2label", {})
            self._model_labels = [str(id2label.get(str(i), "中性"))
                                  for i in range(max(map(int, id2label.keys())) + 1)]
            logger.info("[EmotionExtractor] ONNX 模型就绪：%s（%d 类）",
                        model_path.name, len(self._model_labels))
            self._onnx_ready = True
            return True
        except Exception as e:  # 任何加载异常都不阻断直播，回落规则
            logger.warning("[EmotionExtractor] ONNX 加载失败，规则兜底: %s", e)
            self._onnx_ready = False
            return False

    def _onnx_extract(self, text: str) -> Optional[Dict[str, Any]]:
        """模型推理 → 内部情绪；失败/低置信返回 None（触发规则兜底）。"""
        if self._session is None and self._source in ("model", "auto"):
            self._init_onnx()
        if self._session is None:
            return None
        try:
            import numpy as np
            enc = self._tokenizer.encode(text)
            ids = enc.ids[:512]
            # 去掉 tokenizer 自动 padding（尾部 0），避免 mask 覆盖 padding 干扰模型
            while ids and ids[-1] == 0:
                ids.pop()
            if not ids:
                return None
            # 强制首尾 special token（截断时 [SEP] 可能被切掉）
            if ids[0] != self._tokenizer.token_to_id("[CLS]"):
                ids.insert(0, self._tokenizer.token_to_id("[CLS]"))
            if ids[-1] != self._tokenizer.token_to_id("[SEP]"):
                ids.append(self._tokenizer.token_to_id("[SEP]"))
            ids = ids[:512]
            mask = [1] * len(ids)
            feed = {}
            for inp in self._session.get_inputs():
                name, dtype = inp.name, inp.type
                if "input_ids" in name:
                    feed[name] = np.array([ids], dtype=np.int64)
                elif "attention" in name:
                    feed[name] = np.array([mask], dtype=np.int64)
                elif "token_type" in name or "segment" in name:
                    feed[name] = np.zeros((1, len(ids)), dtype=np.int64)
            out = self._session.run(None, feed)[0]  # (1, num_labels)
            logits = np.asarray(out[0], dtype=np.float64)
            exp = np.exp(logits - logits.max())
            probs = exp / exp.sum()
            idx = int(np.argmax(probs))
            label = self._model_labels[idx] if idx < len(self._model_labels) else "中性"
            score = float(probs[idx])
            if score < self._threshold:
                return None
            emotion = self._labels_map.get(label, "平静")
            return {"emotion": emotion, "score": round(score, 4), "source": "onnx"}
        except Exception as e:
            logger.debug("[EmotionExtractor] ONNX 推理异常，规则兜底: %s", e)
            return None

    # ---------- 命中率统计 ----------

    def _stats(self, dataset: List[Tuple[str, str]], source: str) -> Dict[str, Any]:
        per: Dict[str, Dict[str, int]] = {}
        hit = 0
        for text, expected in dataset:
            if source == "onnx" and not self._onnx_ready:
                return {"hit": 0, "accuracy": 0.0, "total": 0, "per_emotion": {}}
            r = self._onnx_extract(text) if source == "onnx" else self._rule_extract(text)
            ok = r["emotion"] == expected
            hit += 1 if ok else 0
            slot = per.setdefault(expected, {"hit": 0, "total": 0})
            slot["total"] += 1
            slot["hit"] += 1 if ok else 0
        acc = round(hit / len(dataset), 4) if dataset else 0.0
        return {"hit": hit, "accuracy": acc,
                "per_emotion": {k: {"hit": v["hit"], "total": v["total"]} for k, v in per.items()}}

    # ---------- 外部文件加载（训练落点） ----------

    def _load_lexicon(self, path: Path) -> Dict[str, Dict[str, list]]:
        # 深拷贝内置词典：避免追加外部词时污染模块级 EMOTION_WORDS（跨实例泄漏）
        base = {"words": {k: list(v) for k, v in EMOTION_WORDS.items()},
                "punct": dict(EMOTION_PUNCT), "tone": dict(TONE_WORDS)}
        try:
            if path and Path(path).exists():
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                # 外部词表为"增量扩展"：words 列表级追加（不整体覆盖内置词）
                for emotion, words in (data.get("words") or {}).items():
                    slot = base["words"].setdefault(emotion, [])
                    for w in words:
                        if w not in slot:
                            slot.append(w)
                for key in ("punct", "tone"):
                    if isinstance(data.get(key), dict):
                        base[key].update({k: v for k, v in data[key].items()})
                logger.info("[EmotionExtractor] 外部词典已加载：%s", path)
        except Exception as e:
            logger.warning("[EmotionExtractor] 词典加载失败，使用内置: %s", e)
        return base

    def _load_labels_map(self, path: Path) -> Dict[str, str]:
        mapping = dict(MODEL_LABEL_MAP)
        try:
            if path and Path(path).exists():
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    mapping.update({str(k): str(v) for k, v in data.items()})
                logger.info("[EmotionExtractor] 标签映射已加载：%s", path)
        except Exception as e:
            logger.warning("[EmotionExtractor] 标签映射加载失败，使用默认: %s", e)
        return mapping
