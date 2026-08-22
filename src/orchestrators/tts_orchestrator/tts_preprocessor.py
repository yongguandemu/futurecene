"""tts_preprocessor.py — TTS 文本预处理（任务二）

清洗 + 情感参数映射：
- clean(text)：压缩空白/叠音、去颜文字/emoji、截断超长（默认 120 字）
- map_mood(mood)：BatchPlanner 情绪标签 → 归一化情感 + Wusound emo_switch 五维参数
（未知 mood 回落 default，绝不让脏输入进入 TTS 合成）

# 模块内容清单 — tts_preprocessor

## 1. 模块身份标识
- 所属调度官：tts · tts_preprocessor · 能力 tts:synthesize 的前置清洗

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| max_len | 否 | 120 | int | 清洗后最大字符数（截断） |

## 3. 输入契约
- clean(text: str) -> str
- map_mood(mood: str) -> {"emotion", "emo_switch"}

## 4. 输出契约
- 成功：clean 返回清洗后文本（空/None 输入返回空串）；map_mood 返回归一化情感 + 五维参数
- 失败：无（纯函数，永不抛错）

## 5. 依赖声明
- 外部服务：无
- 内部模块：无

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 无 | - | 纯函数无错误路径 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | - | 无状态 |

## 8. 领域状态说明
- 状态项：无
- 持久化：无
"""
import re
from typing import Dict

# 常见颜文字/表情符号（保守集合，避免误删正文）
_KAOMOJI_RE = re.compile(
    r"[（(][^（）()]{1,6}[)）]|[Tt]_[Tt]|[Qq][Aq][Qq]|QAQ|qwq|TAT|>_<|O(?:︿|_|o)O|\([^)]*\u6cea[^)]*\)"
)
# Emoji 区间（U+1F000 起，含符号/表情/补充符号）
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]"
)
# 叠音/重复标点压缩：~！？… 连续 → 单字符
_REPEAT_PUNCT_RE = re.compile(r"([~～！!？?…。])\1{2,}")
_WHITESPACE_RE = re.compile(r"\s+")


class TTSPreprocessor:
    """TTS 文本清洗与情感参数映射。"""

    def __init__(self, max_len: int = 120) -> None:
        self._max_len = max(10, int(max_len))

    def clean(self, text: str) -> str:
        """清洗：去颜文字/emoji → 压缩叠音标点 → 压缩空白 → 截断。"""
        if not text:
            return ""
        t = str(text).strip()
        t = _KAOMOJI_RE.sub("", t)
        t = _EMOJI_RE.sub("", t)
        t = _REPEAT_PUNCT_RE.sub(r"\1", t)
        t = _WHITESPACE_RE.sub(" ", t)
        t = t.strip()
        return t[: self._max_len]

    def map_mood(self, mood: str) -> Dict[str, object]:
        """mood → 归一化情感 + Wusound emo_switch 五维参数（未知回落 default）。"""
        mood = (mood or "").strip().lower()
        # BatchPlanner/ActiveDialogue 情绪 → Wusound 支持集合
        _MAP = {
            "happy": ("happy", [0, 7, 0, 0, 3]),
            "calm": ("calm", [0, 0, 5, 0, 3]),
            "sad": ("sad", [0, 0, 2, 6, 3]),
            "shy": ("shy", [0, 2, 3, 0, 5]),
            "angry": ("angry", [7, 0, 0, 0, 3]),
            "curious": ("calm", [0, 0, 5, 0, 3]),    # 好奇 → 温和中性
            "surprised": ("happy", [0, 7, 0, 0, 3]), # 惊讶 → 高唤醒正面
            "default": ("default", [0, 0, 0, 0, 0]),
        }
        emotion, emo_switch = _MAP.get(mood, _MAP["default"])
        return {"emotion": emotion, "emo_switch": list(emo_switch)}
