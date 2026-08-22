"""test_tts_preprocessor.py — TTS 文本预处理（清洗 + 情感参数映射）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.tts_orchestrator.tts_preprocessor import TTSPreprocessor


def _p(**kw):
    return TTSPreprocessor(**kw)


def test_clean_whitespace():
    assert _p().clean("  今天   天气  不错  ") == "今天 天气 不错"


def test_clean_repeat_punct():
    assert _p().clean("大家好啊！！！～～～") == "大家好啊！～"


def test_clean_kaomoji_and_emoji():
    t = _p().clean("开心(≧▽≦) 太棒了 🎉")
    assert "(≧▽≦)" not in t and "🎉" not in t
    assert "太棒了" in t


def test_clean_truncate():
    assert len(_p(max_len=10).clean("一二三四五六七八九十十一十二十三")) == 10


def test_clean_empty_and_none():
    assert _p().clean("") == ""
    assert _p().clean(None) == ""


def test_map_mood_known():
    r = _p().map_mood("happy")
    assert r["emotion"] == "happy"
    assert r["emo_switch"] == [0, 7, 0, 0, 3]
    assert _p().map_mood("curious")["emotion"] == "calm"   # 好奇 → 温和
    assert _p().map_mood("surprised")["emotion"] == "happy"


def test_map_mood_unknown_default():
    r = _p().map_mood("whatever")
    assert r["emotion"] == "default"
    assert r["emo_switch"] == [0, 0, 0, 0, 0]
    assert _p().map_mood("")["emotion"] == "default"
    assert _p().map_mood(None)["emotion"] == "default"
