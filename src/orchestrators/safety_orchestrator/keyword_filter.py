"""keyword_filter.py — 敏感词规则过滤（可热重载，规格书 5.4）

规则来源：data/safety/rules.json（{"block": [...], "flag": [...]}）+ 内置默认规则。
热重载：reload() 检查文件 mtime，变更时重新加载。
# TODO: 确认 — 完整敏感词库从旧项目 data/safety_model/ 迁移；内置列表仅演示。

# 模块内容清单（8 项契约）
1. 模块身份标识：safety · KeywordFilter · 能力 safety:check_input/check_output 规则层
2. 配置契约：data/safety/rules.json（{"block": [...], "flag": [...]}）+ 内置默认规则
3. 输入契约：check(text) 文本
4. 输出契约：(verdict, matched_words)，verdict ∈ allow/block/flag
5. 依赖声明：json、logging、threading、pathlib、typing、src.shared.config_loader（PROJECT_ROOT）
6. 错误定义：规则文件 JSON 解析失败/OSError 时保留现有规则并记录日志
7. 生命周期方法：reload() 热重载；_load_if_changed() 按 mtime 检查
8. 领域状态说明：_rules 规则表、_mtime 文件修改时间、_lock 线程锁
"""
import json
import logging
import threading
from pathlib import Path
from typing import Dict, List, Tuple

from src.shared.config_loader import PROJECT_ROOT

logger = logging.getLogger(__name__)

RULES_FILE = PROJECT_ROOT / "data" / "safety" / "rules.json"

# 内置默认规则（演示用，待旧项目词库迁移）
DEFAULT_RULES: Dict[str, List[str]] = {
    "block": ["赌博", "博彩", "色情", "毒品", "枪支", "自杀", "暴恐", "法轮功"],
    "flag": ["政治", "敏感"],
}


class KeywordFilter:
    """敏感词规则过滤器。"""

    def __init__(self, rules_file: str = ""):
        self._path = Path(rules_file) if rules_file else RULES_FILE
        self._rules: Dict[str, List[str]] = {
            "block": list(DEFAULT_RULES["block"]),
            "flag": list(DEFAULT_RULES["flag"]),
        }
        self._mtime: float = 0.0
        self._lock = threading.RLock()
        self._load_if_changed()

    def check(self, text: str) -> Tuple[str, List[str]]:
        """检查文本。返回 (verdict, matched_words)；verdict ∈ allow/block/flag。"""
        self._load_if_changed()
        with self._lock:
            for word in self._rules["block"]:
                if word and word in text:
                    return "block", [word]
            for word in self._rules["flag"]:
                if word and word in text:
                    return "flag", [word]
        return "allow", []

    def reload(self) -> int:
        """强制重载规则文件；返回加载的规则词总数。"""
        with self._lock:
            self._rules = {
                "block": list(DEFAULT_RULES["block"]),
                "flag": list(DEFAULT_RULES["flag"]),
            }
            self._mtime = 0.0
        self._load_if_changed()
        return len(self._rules["block"]) + len(self._rules["flag"])

    def _load_if_changed(self) -> None:
        if not self._path.exists():
            return
        try:
            mtime = self._path.stat().st_mtime
            if mtime <= self._mtime:
                return
            data = json.loads(self._path.read_text(encoding="utf-8"))
            with self._lock:
                self._rules = {
                    "block": list(data.get("block", [])),
                    "flag": list(data.get("flag", [])),
                }
                self._mtime = mtime
            logger.info("[KeywordFilter] 规则已热重载: block=%d flag=%d",
                        len(self._rules["block"]), len(self._rules["flag"]))
        except (json.JSONDecodeError, OSError) as e:
            logger.error("[KeywordFilter] 规则加载失败（保留现有规则）: %s", e)
