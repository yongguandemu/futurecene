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


@dataclass
class CharacterProfile:
    role: str
    display_name: str = ""
    system_prompt: str = ""
    keywords: Dict[str, List[str]] = field(default_factory=dict)
    voice_id: str = ""
    catchphrases: List[Dict] = field(default_factory=list)


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
        if not self._dir.exists():
            return []
        return sorted(p.name for p in self._dir.iterdir() if p.is_dir())

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
                profile.display_name = data.get("display_name", role)
                char = data.get("character", {}) or {}
                profile.keywords = self._derive_keywords(data, char)
            except Exception as e:
                logger.warning("[CharacterProfile] %s character.yaml 解析失败: %s", role, e)
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
        self._cache[role] = profile
        return profile

    def keywords_for(self, role: str) -> Dict[str, List[str]]:
        p = self.load(role)
        return p.keywords if p else {}

    @staticmethod
    def _derive_keywords(data: dict, char: dict) -> Dict[str, List[str]]:
        """优先用 character.yaml 的 keywords 字段；缺失则兜底推导。"""
        kw = data.get("keywords") or {}
        if kw:
            return {"personality": list(kw.get("personality", []) or []),
                    "topics": list(kw.get("topics", []) or []),
                    "patterns": list(kw.get("patterns", []) or [])}
        derived = []
        for label in (char.get("personality", []) or []):
            derived.append(str(label))
        if char.get("catchphrase"):
            derived.append(str(char["catchphrase"]).strip("~～,，。"))
        for token in re.split(r"[，,。;；\s]+", str(char.get("speaking_style", ""))):
            token = token.strip()
            if token and len(token) >= 2:
                derived.append(token)
        return {"personality": [str(x) for x in (char.get("personality", []) or [])],
                "topics": derived, "patterns": []}
