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
EMOTION_PUNCT = {"！": "惊讶", "!": "惊讶", "？": "惊讶", "…": "难过", "...": "难过"}
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
