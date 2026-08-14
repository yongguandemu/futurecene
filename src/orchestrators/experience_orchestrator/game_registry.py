"""game_registry.py — 游戏注册表（游戏经验学习域）

游戏名 → 知识库。一套机制，每游戏一份画像+手册。

# 模块内容清单 — game_registry

## 1. 模块身份标识
- 所属调度官：experience
- 能力名：experience:knowledge

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| _PROFILES_DIR | 否 | game_profiles/ | str | 画像目录（缺省路径） |
| _DOCS_ROOT | 否 | docs/ | str | 手册根目录 |

## 3. 输入契约
- 输入格式：`register_game(game, profile_path, docs_dir)` / `get_game(game)` / `list_games()` / `load_default_games()`
- game：str，游戏名（唯一标识）
- profile_path：可选，str，画像路径（缺省用 game_profiles/{game}.json）
- docs_dir：可选，str，手册目录（缺省用 docs/{game}）

## 4. 输出契约
- 成功：`register_game()` 返回 bool（加载成功 True）；`get_game()` 返回 GameKnowledge 或 `None`；`list_games()` 返回排序后的游戏名列表
- 失败：`register_game()` 画像加载失败返回 `False`（不注册）
- 事件：无

## 5. 依赖声明
- 外部服务：本地文件系统（画像/手册文件）
- 内部模块：`game_knowledge.GameKnowledge`、`src/shared/config_loader.PROJECT_ROOT`
- 预先配置：启动时调用 load_default_games 注册内置游戏（minecraft；vn 若画像存在）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 画像缺失 | 画像文件不存在或加载失败 | register_game 返回 False，不注册 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start/stop | 否 | 无（模块级缓存，启动时 load_default_games 注册） |

## 8. 领域状态说明
- 状态项：`_REGISTRY`（game → GameKnowledge 模块级缓存）
- 持久化：无（注册表内存态，重启需重新注册）
- 恢复：load_default_games 重建内置游戏注册
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