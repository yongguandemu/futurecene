"""test_emotion_extractor.py — 情绪提取（规则兜底 + ONNX 模型）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.live2d_orchestrator.emotion_extractor import EmotionExtractor


def _isolated(**kw):
    """隔离工作区 lexicon/labels_map，测试不依赖真实模型目录文件。"""
    kw.setdefault("lexicon_path", Path(__file__).parent / "_nonexist_lexicon.json")
    kw.setdefault("labels_map_path", Path(__file__).parent / "_nonexist_labels.json")
    return EmotionExtractor(**kw)


def _e(text):
    return _isolated(source="rule").extract(text)


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


def test_source_invalid_falls_back_to_auto():
    """非法 source 值回落 auto（不抛错）。"""
    e = _isolated(source="invalid")
    assert e._source == "auto"


def test_source_model_degrade_when_missing():
    """source=model 且模型目录为空时降级规则（不抛错、可提取）。"""
    e = _isolated(source="model", model_dir=Path(__file__).parent / "_no_such_dir")
    r = e.extract("今天好开心啊！")
    assert r["emotion"] == "开心"
    assert r["source"] == "rule"


# ---------- ONNX 集成（模型就绪时验证真实推理链路） ----------

def _onnx_extractor():
    """返回将走 ONNX 推理的 extractor；环境（依赖/模型文件）缺失时返回 None。"""
    import importlib.util
    e = _isolated(source="auto")
    if importlib.util.find_spec("onnxruntime") is None:
        return None
    if importlib.util.find_spec("tokenizers") is None:
        return None
    if not (e._model_dir / "model.onnx").exists():
        return None
    return e  # extract 时惰性加载模型


def test_onnx_happy_text():
    e = _onnx_extractor()
    if e is None:
        import pytest
        pytest.skip("ONNX 模型未就绪")
    r = e.extract("今天好开心啊！")
    assert r["emotion"] == "开心"
    assert r["source"] == "onnx"
    assert 0.0 <= r["score"] <= 1.0


def test_onnx_sad_and_surprised():
    e = _onnx_extractor()
    if e is None:
        import pytest
        pytest.skip("ONNX 模型未就绪")
    assert e.extract("呜呜呜好难过")["emotion"] == "难过"
    assert e.extract("什么？！竟然是这样")["emotion"] == "惊讶"


def test_onnx_label_map_fear_to_surprised():
    """害怕 → 惊讶（默认 labels_map 覆盖生效）。"""
    e = _onnx_extractor()
    if e is None:
        import pytest
        pytest.skip("ONNX 模型未就绪")
    assert e.extract("好可怕，吓死我了")["emotion"] == "惊讶"
