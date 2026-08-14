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
