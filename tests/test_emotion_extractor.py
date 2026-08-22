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


def test_tone_word_tiebreak():
    r = _e("哼")
    assert r["emotion"] == "生气"
