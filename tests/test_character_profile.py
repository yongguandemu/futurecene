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
            "  speaking_style: 犀利，毒\n",
            encoding="utf-8")
        (r / "system_prompt.txt").write_text("你是Lilith。", encoding="utf-8")
        loader = CharacterProfileLoader(profiles_dir=Path(d) / "profiles")
        kw = loader.keywords_for("lilith")
        # 强断言：topics 桶含 catchphrase 剥离后的词（"呵"）与 speaking_style 分词（"犀利"）
        assert "呵" in kw["topics"]
        assert "犀利" in kw["topics"]
        # len<2 的单字（speaking_style 分词噪声"毒"）被过滤
        assert "毒" not in kw["topics"]
        # personality 只进 personality 桶，不与 topics 重复计分（RelevanceRule 权重依赖）
        assert "毒舌" in kw["personality"] and "毒舌" not in kw["topics"]
        assert "冷静" in kw["personality"] and "冷静" not in kw["topics"]


def test_load_behavior_rules():
    """behavior_rules.yaml 被正确加载到 profile.behavior_rules。"""
    with tempfile.TemporaryDirectory() as d:
        _make_profiles(Path(d))
        r = Path(d) / "profiles" / "yuki"
        (r / "behavior_rules.yaml").write_text(
            "rules:\n"
            "  preferred_topics:\n"
            "    - 日常聊天\n"
            "    - 动漫游戏\n"
            "  avoid_topics:\n"
            "    - 政治敏感\n"
            "  live:\n"
            "    auto_greet_new_viewers: true\n",
            encoding="utf-8")
        loader = CharacterProfileLoader(profiles_dir=Path(d) / "profiles")
        p = loader.load("yuki")
        assert p.behavior_rules
        rules = p.behavior_rules.get("rules", {})
        assert "日常聊天" in rules.get("preferred_topics", [])
        assert "动漫游戏" in rules.get("preferred_topics", [])
        assert "政治敏感" in rules.get("avoid_topics", [])
        assert rules.get("live", {}).get("auto_greet_new_viewers") is True


def test_behavior_rules_empty_when_missing():
    """behavior_rules.yaml 不存在时 behavior_rules 为空 dict。"""
    with tempfile.TemporaryDirectory() as d:
        _make_profiles(Path(d))
        loader = CharacterProfileLoader(profiles_dir=Path(d) / "profiles")
        p = loader.load("yuki")
        assert p.behavior_rules == {}


def test_load_caches_same_object_and_ignores_file_change():
    with tempfile.TemporaryDirectory() as d:
        _make_profiles(Path(d))
        loader = CharacterProfileLoader(profiles_dir=Path(d) / "profiles")
        p1 = loader.load("yuki")
        p2 = loader.load("yuki")
        assert p1 is p2  # 二次 load 命中缓存：同一对象，不重读
        (Path(d) / "profiles" / "yuki" / "character.yaml").write_text(
            "display_name: Changed\n"
            "character:\n"
            "  personality: [高冷]\n",
            encoding="utf-8")
        p3 = loader.load("yuki")
        assert p3 is p1
        assert p3.display_name == "Yuki"  # 文件改动后仍为缓存值，未重读


def test_load_malformed_yaml_degrades_gracefully():
    with tempfile.TemporaryDirectory() as d:
        r = Path(d) / "profiles" / "nino"
        r.mkdir(parents=True)
        (r / "character.yaml").write_text("display_name: [unclosed\n",
                                          encoding="utf-8")  # 语法错误
        (r / "system_prompt.txt").write_text("你是Nino。", encoding="utf-8")
        (r / "tts_config.yaml").write_text("tts:\n  wusound:\n    voice_id: v-nino\n",
                                           encoding="utf-8")
        loader = CharacterProfileLoader(profiles_dir=Path(d) / "profiles")
        p = loader.load("nino")  # 畸形 YAML 不抛异常
        assert p is not None
        assert p.display_name == "nino"  # 降级回退角色名
        assert p.system_prompt == "你是Nino。"  # 其余字段仍可加载
        assert p.voice_id == "v-nino"
        assert p.keywords == {}


def test_fallback_minor_robustness():
    with tempfile.TemporaryDirectory() as d:
        r = Path(d) / "profiles" / "mio"
        r.mkdir(parents=True)
        (r / "character.yaml").write_text(
            "display_name: \"\"\n"
            "character:\n"
            "  personality: 活泼\n"   # 缺 []：字符串标签
            "  catchphrase: 耶~\n"
            "  speaking_style: null\n",  # None：不产生幽灵关键词
            encoding="utf-8")
        (r / "system_prompt.txt").write_text("你是Mio。", encoding="utf-8")
        loader = CharacterProfileLoader(profiles_dir=Path(d) / "profiles")
        p = loader.load("mio")
        assert p.display_name == "mio"  # 空串回退 role
        assert p.keywords["personality"] == ["活泼"]  # 字符串包成 list，不拆成单字
        assert "活泼" not in p.keywords["topics"]  # 双桶去重
        assert "None" not in p.keywords["topics"]  # speaking_style=None 无幽灵词
        assert "耶" in p.keywords["topics"]  # catchphrase 去语气词/标点后保留


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
