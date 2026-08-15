"""character_profile.py — 角色配置加载器（config/profiles/{role}/）

读取 character.yaml（v2.1 含 keywords）/ system_prompt.txt / tts_config.yaml / catchphrases.json。
keywords 缺失时从 personality/catchphrase/speaking_style 兜底推导（零配置可降级）。
"""
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

PROFILES_DIR = Path(__file__).resolve().parents[2] / "config" / "profiles"

# 兜底推导用：catchphrase 剥离的句尾语气词与标点（"呵/哼/耶"等口癖叹词保留）
_PARTICLE_CHARS = "啊呀哦噢嗯呢吧嘛么呗哟啦"
_STRIP_CHARS = "~～,，。！!?？…、 "


def _strip_catchphrase(text: str) -> str:
    return text.strip(_PARTICLE_CHARS + _STRIP_CHARS)


@dataclass
class CharacterProfile:
    role: str
    display_name: str = ""
    system_prompt: str = ""
    keywords: Dict[str, List[str]] = field(default_factory=dict)
    voice_id: str = ""
    catchphrases: List[Dict] = field(default_factory=list)
    behavior_rules: Dict = field(default_factory=dict)


class CharacterProfileLoader:
    """按角色加载配置；缺失角色返回 None。"""

    def __init__(self, profiles_dir: Optional[Path] = None, yaml=None, json=None):
        self._dir = Path(profiles_dir) if profiles_dir else PROFILES_DIR
        self._yaml = yaml
        self._json = json
        self._cache: Dict[str, Optional[CharacterProfile]] = {}

    def _imports(self):
        if self._yaml is None or self._json is None:
            import json
            import yaml
            self._yaml = yaml
            self._json = json
        return self._yaml, self._json

    def all_roles(self) -> List[str]:
        """全部可用角色：目录枚举，排除停用角色（character.yaml enabled: false，如改名遗留 lumi）。"""
        if not self._dir.exists():
            return []
        roles = []
        for p in self._dir.iterdir():
            if not p.is_dir():
                continue
            cy = p / "character.yaml"
            enabled = True
            if cy.exists():
                try:
                    yaml, _ = self._imports()
                    data = yaml.safe_load(cy.read_text(encoding="utf-8")) or {}
                    enabled = data.get("enabled", True) is not False
                except Exception:
                    pass
            if enabled:
                roles.append(p.name)
        return sorted(roles)

    def load(self, role: str) -> Optional[CharacterProfile]:
        if role in self._cache:
            return self._cache[role]
        role_dir = self._dir / role
        if not role_dir.is_dir():
            self._cache[role] = None
            return None
        yaml, json_mod = self._imports()
        profile = CharacterProfile(role=role)
        char_yaml = role_dir / "character.yaml"
        if char_yaml.exists():
            try:
                data = yaml.safe_load(char_yaml.read_text(encoding="utf-8")) or {}
                profile.display_name = data.get("display_name") or role
                char = data.get("character", {}) or {}
                profile.keywords = self._derive_keywords(data, char)
            except Exception as e:
                logger.warning("[CharacterProfile] %s character.yaml 解析失败: %s", role, e)
                profile.display_name = role  # 畸形 YAML 降级：至少回退角色名
        sp = role_dir / "system_prompt.txt"
        if sp.exists():
            profile.system_prompt = sp.read_text(encoding="utf-8").strip()
        tts_yaml = role_dir / "tts_config.yaml"
        if tts_yaml.exists():
            try:
                tts = yaml.safe_load(tts_yaml.read_text(encoding="utf-8")) or {}
                wu = tts.get("tts", {}).get("wusound", {}) or {}
                profile.voice_id = wu.get("voice_id", "")
            except Exception:
                pass
        cp = role_dir / "catchphrases.json"
        if cp.exists():
            try:
                profile.catchphrases = (json_mod.loads(cp.read_text(encoding="utf-8"))
                                        .get("phrases", []))
            except Exception:
                pass
        br = role_dir / "behavior_rules.yaml"
        if br.exists():
            try:
                profile.behavior_rules = yaml.safe_load(br.read_text(encoding="utf-8")) or {}
            except Exception as e:
                logger.warning("[CharacterProfile] %s behavior_rules.yaml 解析失败: %s", role, e)
        self._cache[role] = profile
        return profile

    def keywords_for(self, role: str) -> Dict[str, List[str]]:
        p = self.load(role)
        return p.keywords if p else {}

    @staticmethod
    def _derive_keywords(data: dict, char: dict) -> Dict[str, List[str]]:
        """优先用 character.yaml 的 keywords 字段；缺失则兜底推导。

        兜底规则（RelevanceRule 依赖三桶权重区分，同一标签不得双桶重复计分）：
        - personality 标签只进 personality 桶；
        - topics 桶只收 catchphrase（去语气词/标点后的内容词）与 speaking_style 分词；
        - speaking_style 分词按 len>=2 过滤单字噪声；catchphrase 为刻意的口癖，单字保留；
        - patterns 恒为空。
        """
        kw = data.get("keywords") or {}
        if kw:
            return {"personality": list(kw.get("personality", []) or []),
                    "topics": list(kw.get("topics", []) or []),
                    "patterns": list(kw.get("patterns", []) or [])}
        personality = char.get("personality", []) or []
        if isinstance(personality, str):
            personality = [personality]  # 缺 []：包成单元素 list，避免按字符拆散
        personality_tags = [str(x) for x in personality]
        topics = []
        catchphrase = _strip_catchphrase(str(char.get("catchphrase") or ""))
        if catchphrase:
            topics.append(catchphrase)
        for token in re.split(r"[，,。;；、\s]+",
                              str(char.get("speaking_style") or "")):
            token = token.strip()
            if len(token) >= 2:  # 分词单字噪声被过滤
                topics.append(token)
        return {"personality": personality_tags, "topics": topics, "patterns": []}
