"""test_batch_planner.py — 批量发言计划（LLM 结构化输出 + 降级）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.llm_orchestrator.batch_planner import BatchPlanner
from src.orchestrators.tts_orchestrator.tts_preprocessor import TTSPreprocessor


def _planner(chat_fn=None):
    return BatchPlanner(chat_fn=chat_fn, preprocessor=TTSPreprocessor())


def test_generate_valid_llm_output():
    """LLM 返回合法 JSON 数组 → 解析出 count 条完整计划。"""
    def fake(messages):
        return ('[{"text": "大家今天过得怎么样？", "mood": "calm", '
                '"suggested_window_sec": 90, "duration_estimate": 6.0},'
                '{"text": "我最近在学做菜！", "mood": "happy", '
                '"suggested_window_sec": 120, "duration_estimate": 8.0}]', {})
    plans = _planner(fake).generate(context="直播间", role="yuki", count=2)
    assert len(plans) == 2
    p = plans[0]
    assert {"text", "mood", "suggested_window_sec", "duration_estimate"} == set(p.keys())
    assert p["mood"] == "calm"
    assert p["suggested_window_sec"] == 90
    assert p["duration_estimate"] == 6.0


def test_parse_json_fenced_block():
    """容错解析 ```json 代码块。"""
    def fake(messages):
        return ('```json\n[{"text": "嘿，来聊聊？", "mood": "curious", '
                '"suggested_window_sec": 100, "duration_estimate": 5}]\n```', {})
    plans = _planner(fake).generate(count=1)
    assert len(plans) == 1
    assert plans[0]["text"] == "嘿，来聊聊？"


def test_invalid_mood_falls_back_default():
    """非法 mood → default。"""
    def fake(messages):
        return ('[{"text": "测试文本", "mood": "angry_very", '
                '"suggested_window_sec": 50, "duration_estimate": 4}]', {})
    plans = _planner(fake).generate(count=1)
    assert plans[0]["mood"] == "default"


def test_numeric_clamping():
    """window/duration 越界钳制到范围。"""
    def fake(messages):
        return ('[{"text": "钳制测试", "mood": "happy", '
                '"suggested_window_sec": 9999, "duration_estimate": 0.5}]', {})
    p = _planner(fake).generate(count=1)[0]
    assert p["suggested_window_sec"] == 300
    assert p["duration_estimate"] == 3.0


def test_fallback_without_chat_fn():
    """chat_fn 未注入 → 话题池单条兜底（≥1 条，字段完整）。"""
    plans = _planner(None).generate(context="", role="", count=3)
    assert len(plans) == 3
    assert all(p["mood"] == "default" for p in plans)
    assert all(p["text"] for p in plans)


def test_llm_failure_falls_back():
    """LLM 调用异常 → 话题池兜底不崩。"""
    def boom(messages):
        raise RuntimeError("engine down")
    plans = _planner(boom).generate(count=2)
    assert len(plans) == 2
    assert all(p["text"] for p in plans)


def test_llm_garbage_output_falls_back():
    """LLM 输出非 JSON → 解析失败兜底。"""
    def junk(messages):
        return ("不好意思我今天不想说话", {})
    plans = _planner(junk).generate(count=2)
    assert len(plans) == 2
    assert all(p["text"] for p in plans)


def test_clean_text_via_preprocessor():
    """文本经 TTS Preprocessor 清洗（去叠音标点/颜文字）。"""
    def fake(messages):
        return ('[{"text": "哈哈哈！！！ 大家好啊(≧▽≦)", "mood": "happy", '
                '"suggested_window_sec": 60, "duration_estimate": 5}]', {})
    p = _planner(fake).generate(count=1)[0]
    assert "！！！" not in p["text"]
    assert "(≧▽≦)" not in p["text"]
