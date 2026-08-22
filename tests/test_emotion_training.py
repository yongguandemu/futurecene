"""test_emotion_training.py — 文本→情绪→参数命中率训练机制（词典扩展/标签映射/评测）"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.live2d_orchestrator.emotion_extractor import (
    EmotionExtractor,
    MODEL_LABEL_MAP,
)
from scripts.train_emotion_mapping import learn_lexicon


def _isolated(**kw):
    """隔离工作区 lexicon/labels_map，测试不依赖真实模型目录文件。"""
    kw.setdefault("lexicon_path", Path(__file__).parent / "_nonexist_lexicon.json")
    kw.setdefault("labels_map_path", Path(__file__).parent / "_nonexist_labels.json")
    return EmotionExtractor(**kw)


def test_evaluate_structure_and_accuracy(tmp_path):
    """evaluate 返回结构完整，rule 命中率按期望统计。"""
    e = _isolated(source="rule")
    dataset = [("今天好开心啊！", "开心"), ("气死我了", "生气"), ("嗯，好的", "平静")]
    stats = e.evaluate(dataset)
    assert stats["total"] == 3
    assert stats["rule"]["hit"] == 3
    assert stats["rule"]["accuracy"] == 1.0
    assert stats["rule"]["per_emotion"]["开心"] == {"hit": 1, "total": 1}
    assert stats["onnx"]["hit"] == 0  # rule 模式不加载模型
    assert stats["samples"] == []


def test_evaluate_reports_mismatch(tmp_path):
    """evaluate 统计结构完整（rule 模式不依赖模型也能产出报告）。"""
    e = _isolated(source="auto")
    dataset = [("气死我了", "生气"), ("嗯，好的", "平静")]
    stats = e.evaluate(dataset)
    assert stats["total"] == 2
    assert set(stats.keys()) == {"total", "rule", "onnx", "samples"}
    assert stats["rule"]["hit"] >= 1
    assert 0.0 <= stats["rule"]["accuracy"] <= 1.0


def test_lexicon_json_extends_words(tmp_path):
    """外部 lexicon.json 扩展规则词库（训练词典落点，增量合并不覆盖内置）。"""
    lexicon = tmp_path / "lexicon.json"
    lexicon.write_text(json.dumps({
        "words": {"开心": ["起飞了"]},
        "punct": {"#": "惊讶"},
        "tone": {"哦": "平静"},
    }, ensure_ascii=False), encoding="utf-8")
    e = EmotionExtractor(source="rule", lexicon_path=lexicon)
    r = e.extract("今天直接起飞了")
    assert r["emotion"] == "开心"
    assert "起飞了" in e._lexicon["words"]["开心"]
    assert "开心" in e._lexicon["words"]["开心"]  # 内置词保留
    assert e._lexicon["punct"]["#"] == "惊讶"
    assert e._lexicon["tone"]["哦"] == "平静"


def test_lexicon_broken_file_keeps_builtin(tmp_path):
    """词典文件损坏时不崩溃，回落内置词典。"""
    bad = tmp_path / "lexicon.json"
    bad.write_text("{not json", encoding="utf-8")
    e = EmotionExtractor(source="rule", lexicon_path=bad)
    assert "开心" in e._lexicon["words"]
    assert "开心" in e._lexicon["words"]["开心"]


def test_labels_map_custom_override(tmp_path):
    """labels_map.json 可覆盖模型标签→内部情绪映射（对应关系可调优）。"""
    mapping = tmp_path / "labels_map.json"
    mapping.write_text(json.dumps({"关心": "害羞"}), encoding="utf-8")
    e = EmotionExtractor(source="rule", labels_map_path=mapping)
    assert e._labels_map["关心"] == "害羞"
    assert e._labels_map["开心"] == "开心"          # 未覆盖项保留默认
    assert MODEL_LABEL_MAP["关心"] == "开心"        # 模块默认不受影响


def test_learn_lexicon_from_model_correct_rule_wrong():
    """词典学习：仅从"模型正确且规则错误"的样本提取 2-gram。"""
    e = _isolated(source="rule")
    # 打桩：假装模型就绪且对样本给出期望情绪
    e._onnx_ready = True
    e._onnx_extract = lambda t: {"emotion": "开心", "score": 0.9, "source": "onnx"}
    dataset = [
        ("今天直接起飞了", "开心"),   # 规则未命中（无"起飞"词）→ 学词
        ("好耶好耶", "开心"),        # 规则已命中 → 跳过
        ("嗯嗯", "平静"),            # 规则命中（平静）→ 跳过
    ]
    new_words = learn_lexicon(dataset, e, min_count=1)
    assert "起飞" in new_words.get("开心", [])
    assert "耶" not in "".join(new_words.get("开心", []))  # 已命中样本不学


def test_learn_lexicon_empty_without_model():
    """模型未就绪时学习结果为空（不产生噪音词）。"""
    e = _isolated(source="rule")  # _onnx_ready=False
    new_words = learn_lexicon([("今天直接起飞了", "开心")], e, min_count=1)
    assert all(v == [] for v in new_words.values())
