"""game_knowledge.py — 混合游戏知识库（游戏经验学习域）

结构化画像（高频小知识，常驻注入）+ 自然语言手册（按需检索）。
通用机制一套代码，游戏特定内容在画像 JSON 与 docs 目录。

# 模块内容清单（8 项契约摘录）
- 所属调度官：experience
- 能力名：experience:decide 的知识注入来源
- 配置契约：profile_path(必填 json) / docs_dir(可选)
- 输入契约：inject_decision() / search_docs(query, top_k)
- 输出契约：纯文本注入块 / 手册检索文本
- 生命周期：load() 加载；领域状态：画像缓存，重启需重新 load
"""
import json
import os
import logging

logger = logging.getLogger(__name__)


class GameKnowledge:
    """混合游戏知识库：画像 + 手册。"""

    def __init__(self, profile_path: str, docs_dir: str = None):
        self._profile_path = profile_path
        self._docs_dir = docs_dir
        self._data = {}
        self._loaded = False

    def load(self) -> bool:
        try:
            with open(self._profile_path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            if "window" not in self._data:
                logger.warning("[GameKnowledge] 画像缺 window 字段: %s", self._profile_path)
                return False
            self._loaded = True
            logger.info("[GameKnowledge] 已加载 %s 画像",
                        (self._data or {}).get("game", "?"))
            return True
        except Exception as e:
            logger.warning("[GameKnowledge] 画像加载失败 %s: %s", self._profile_path, e)
            return False

    # ---- 注入 ----
    def inject_decision(self) -> str:
        """画像的 keys/rules/ui_guide → 文本块（注入探索 prompt）。"""
        if not self._loaded:
            return ""
        parts = []
        d = self._data
        keys = d.get("keys") or {}
        if keys:
            parts.append("【操作键位】" + " ".join(
                "{}={}".format(k, v) for k, v in keys.items()))
        rules = d.get("rules") or []
        if rules:
            parts.append("【运行规则】" + "；".join(rules))
        ui = d.get("ui_guide") or {}
        if ui:
            parts.append("【界面指南】" + " ".join(
                "{}:{}".format(k, v) for k, v in ui.items()))
        return "\n".join(parts)

    def inject_constraints(self) -> dict:
        """画像 constraints 字段（决策约束，如操作频率上限）。"""
        return (self._data.get("constraints") or {}) if self._loaded else {}

    def search_docs(self, query: str, top_k: int = 2) -> str:
        """手册检索：query 与 docs topic 匹配 → 读文件前 N 行。未命中返回空。"""
        if not self._loaded or not self._docs_dir:
            return ""
        docs = self._data.get("docs") or []
        if not docs:
            return ""
        hits = []
        for doc in docs:
            topic = (doc.get("topic") or "").lower()
            if topic and topic in (query or "").lower():
                hits.append(doc)
        if not hits:
            return ""
        out = []
        for doc in hits[:top_k]:
            ref = doc.get("ref", "")
            fp = os.path.join(self._docs_dir, ref) if ref else ""
            if fp and os.path.exists(fp):
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        lines = f.read().splitlines()[:12]
                    out.append("【{}】{}".format(doc.get("topic"), " ".join(lines)))
                except Exception:
                    continue
        return "\n".join(out)

    # ---- 属性 ----
    @property
    def game(self) -> str:
        return (self._data or {}).get("game", "")

    @property
    def window(self) -> dict:
        return (self._data or {}).get("window", {}) if self._loaded else {}

    @property
    def loaded(self) -> bool:
        return self._loaded