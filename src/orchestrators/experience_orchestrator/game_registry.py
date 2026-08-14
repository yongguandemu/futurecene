"""game_registry.py — 游戏注册表（游戏经验学习域）

游戏名 → 知识库。一套机制，每游戏一份画像+手册。

# 模块内容清单（8 项契约摘录）
- 所属调度官：experience
- 能力名：experience:knowledge
- 配置契约：_PROFILES_DIR(profile 目录) / _DOCS_ROOT(docs 目录)
- 输入契约：register_game(game, profile_path, docs_dir)
- 输出契约：get_game(game) -> GameKnowledge / list_games() -> list
- 生命周期：模块级缓存，启动时注册内置游戏
"""
import os
import logging

from src.orchestrators.experience_orchestrator.game_knowledge import GameKnowledge
from src.shared.config_loader import PROJECT_ROOT

logger = logging.getLogger(__name__)

_PROFILES_DIR = str(PROJECT_ROOT / "game_profiles")
_DOCS_ROOT = str(PROJECT_ROOT / "docs")

_REGISTRY = {}   # game -> GameKnowledge


def register_game(game: str, profile_path: str = None, docs_dir: str = None) -> bool:
    """注册游戏：加载画像。profile_path 缺省用 game_profiles/<game>.json。"""
    pp = profile_path or os.path.join(_PROFILES_DIR, "{}.json".format(game))
    dd = docs_dir or os.path.join(_DOCS_ROOT, game)
    gk = GameKnowledge(pp, docs_dir=dd if os.path.isdir(dd) else None)
    if not gk.load():
        return False
    _REGISTRY[game] = gk
    logger.info("[Registry] 已注册游戏: %s", game)
    return True


def get_game(game: str) -> GameKnowledge:
    return _REGISTRY.get(game)


def list_games() -> list:
    return sorted(_REGISTRY.keys())


def load_default_games():
    """启动时注册内置游戏（minecraft；vn 若画像存在）。"""
    register_game("minecraft")
    if os.path.exists(os.path.join(_PROFILES_DIR, "vn.json")):
        register_game("vn")