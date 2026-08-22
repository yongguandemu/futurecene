"""world_book.py — 世界书加载、检索与自动进化（shared 跨域数据访问）

数据源：data/worldbook.json（旧系统 LumiProject 470 条 v2 格式完整迁移，P0）。
能力：CRUD（增删改查 + 落盘持久化）、检索（角色/分类/关键词）、
实时自动进化（订阅弹幕/礼物事件 → 生成观众话题/重要观众/常驻观众条目）、
增强（建议/合并/手动进化/演化日志/导出/统计）。

消费方（联通）：
- commander（danmaku_pipeline._system_prompt / command_router._inject_llm_context）：
  组装 system_prompt 时追加本角色核心条目（character/relationship/behavior）
- live_intelligence（context_aggregator._collect_world_book_entries）：
  情境聚合 world_book_entries 槽位，供 AI 决策参考
接口兼容 context_aggregator 消费协议：get_entries_by_category() / get_enabled_entries()。

# 模块内容清单 — world_book

## 1. 模块身份标识
- 所属调度官：shared（跨域数据访问层，被 commander / live_intelligence / 装配层消费）
- 能力名：无（数据访问 + 自动进化服务，非调度官能力；进化由装配层 start_evolving 启动，D3 被动）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| path | 否 | <项目根>/data/worldbook.json | str | 世界书文件路径（P0 迁移落点） |
| evolve.enabled | 否 | True | bool | 自动进化总开关 |
| evolve.topic_threshold | 否 | 10 | int | 话题出现次数 ≥ 该值 → 观众话题条目 |
| evolve.viewer_gift_value | 否 | 100 | int | 观众累计礼物价值（金瓜子）≥ 该值 → 重要观众条目 |
| evolve.viewer_msg_count | 否 | 20 | int | 观众发言数 ≥ 该值 → 常驻观众条目 |

## 3. 输入契约
- `WorldBook(path=None, evolve_cfg=None)`：构造即加载（文件缺失/解析失败 → 空书 + 警告）
- 检索：`entries_for_role(role)` / `core_entries(role)` / `get_entries_by_category(category)` /
  `get_enabled_entries()` / `search(keyword)` / `system_prompt_block(role, max_chars)`
- 写：`add_entry(id,title,content,...)` / `update_entry(...)` / `remove_entry(id)` / `save_to_disk()`
- 进化：`start_evolving(event_bus, evolve_cfg)` / `stop_evolving()`（订阅 danmaku:received / gift:received）
- 增强：`suggest(context)` / `evolve(event)` / `merge_entries(a,b)` / `export_book()` / `get_evolution_log()`

## 4. 输出契约
- 成功：条目 dict 列表（entry_id/title/content/category/tags/metadata）；写方法返回 bool
- 失败：文件缺失/解析异常 → 空书 + 警告（不阻断启动）；写盘失败返回 False + 警告

## 5. 依赖声明
- 外部服务：无
- 内部模块：json、logging、re、threading、time、pathlib、typing、src.shared.config_loader（PROJECT_ROOT）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 文件缺失 | data/worldbook.json 未迁移 | 空书 + 警告，角色 prompt 退回纯档案 |
| 写盘失败 | 磁盘只读/权限 | save_to_disk 返回 False，进化条目仅内存 |
| 事件订阅失败 | EventBus 异常 | start_evolving 捕获并降级为不进化（不阻断启动） |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start_evolving | 否 | 订阅弹幕/礼物事件自动进化（装配层调用，幂等） |
| stop_evolving | 否 | 退订事件（幂等） |

## 8. 领域状态说明
- 状态项：_entries / _categories / _version / _evolution_log / _topic_freq / _viewer_gift / _viewer_msg
- 持久化：save_to_disk 写回 data/worldbook.json（v2 格式，新增条目自动落盘）
- 恢复：每次构造重新加载；进化统计（话题频率等）为运行时状态，重启清零重新积累
"""
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.shared.config_loader import PROJECT_ROOT
from src.shared.events import DANMAKU_RECEIVED, GIFT_RECEIVED

logger = logging.getLogger(__name__)

DEFAULT_WORLDBOOK_PATH = PROJECT_ROOT / "data" / "worldbook.json"

# 注入 system_prompt 的核心分类（角色世界观/关系/行为，排除运行时动态条目）
CORE_CATEGORIES = ("character", "relationship", "behavior")

# 自动进化默认阈值（config.yaml world_book.evolve 可覆盖）
DEFAULT_EVOLVE_CFG = {
    "topic_threshold": 10,      # 话题出现次数 ≥ 该值 → 生成观众话题条目
    "viewer_gift_value": 100,   # 观众累计礼物价值（金瓜子）≥ 该值 → 重要观众条目
    "viewer_msg_count": 20,     # 观众发言数 ≥ 该值 → 常驻观众条目
    "enabled": True,
}

# 话题分词停用词（与旧系统 WorldBookEvolver.STOP_WORDS 一致）
STOP_WORDS = {"了", "的", "是", "我", "你", "他", "她", "它", "们", "吗", "呢",
              "吧", "啊", "呀", "哦", "嗯", "哈", "就", "都", "也", "在", "有",
              "这", "那", "个", "不", "没", "很", "好", "啊哈", "主播", "直播",
              "今天", "明天", "什么时候", "哈哈哈哈"}


class WorldBook:
    """世界书：加载/CRUD/检索/自动进化（旧系统 WorldBookEvolver 能力并入）。"""

    def __init__(self, path: Optional[Path] = None,
                 evolve_cfg: Optional[Dict[str, Any]] = None):
        self._path = Path(path) if path else DEFAULT_WORLDBOOK_PATH
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._categories: Dict[str, List[str]] = {}
        self._version = 1
        self._evolution_log: List[Dict[str, Any]] = []
        self._lock = threading.RLock()
        # 自动进化状态
        self._bus = None
        self._evolving = False
        self._evolve_cfg = dict(DEFAULT_EVOLVE_CFG)
        if evolve_cfg:
            self._evolve_cfg.update(evolve_cfg)
        self._topic_freq: Dict[str, int] = {}
        self._viewer_gift: Dict[str, Dict[str, Any]] = {}
        self._viewer_msg: Dict[str, Dict[str, Any]] = {}
        self._newly_added = False
        self._load()

    # ================= 加载 / 持久化 =================

    def _load(self) -> None:
        """加载世界书文件（v2 格式：version/entry_count/entries/categories）。"""
        if not self._path.exists():
            logger.warning("[WorldBook] 世界书文件缺失: %s（角色 prompt 退回纯档案）", self._path)
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                book = json.load(f)
            with self._lock:
                for e in book.get("entries", []) or []:
                    eid = e.get("entry_id") or e.get("title", "")
                    if not eid:
                        continue
                    entry = {
                        "entry_id": eid,
                        "title": e.get("title", ""),
                        "content": e.get("content", ""),
                        "category": e.get("category", "general"),
                        "tags": e.get("tags", []) or [],
                        "metadata": e.get("metadata", {}) or {},
                        "created_at": e.get("created_at", time.time()),
                        "updated_at": e.get("updated_at", time.time()),
                        "version": e.get("version", 1),
                    }
                    self._entries[eid] = entry
                    self._categories.setdefault(entry["category"], []).append(eid)
                self._version = int(book.get("version", 1) or 1)
            logger.info("[WorldBook] 世界书加载完成: %d 条 (v%d)",
                        len(self._entries), self._version)
        except Exception as e:
            logger.warning("[WorldBook] 世界书解析失败: %s", e)

    def save_to_disk(self) -> bool:
        """导出并写入 worldbook.json（v2 格式，与旧系统/前端共用文件）。"""
        try:
            book = json.loads(self.export_book())
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(book, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.warning("[WorldBook] 保存世界书失败: %s", e)
            return False

    # ================= 检索（context_aggregator 兼容协议） =================

    def get_entries_by_category(self, category: str) -> List[Dict[str, Any]]:
        """按分类取条目（context_aggregator 兼容协议）。"""
        with self._lock:
            ids = self._categories.get(category, [])
            return [self._entries[i] for i in ids]

    def get_enabled_entries(self) -> List[Dict[str, Any]]:
        """取全部启用条目（协议兼容；世界书无 enabled 概念，返回全量）。"""
        with self._lock:
            return list(self._entries.values())

    def entries_for_role(self, role: str) -> List[Dict[str, Any]]:
        """按 metadata.role 严格过滤条目（yuki/lilith 双角色基线）。"""
        with self._lock:
            return [e for e in self._entries.values()
                    if e.get("metadata", {}).get("role") == role]

    def core_entries(self, role: str) -> List[Dict[str, Any]]:
        """角色核心设定条目：character/relationship/behavior 分类 ∩ metadata.role。"""
        with self._lock:
            return [e for e in self._entries.values()
                    if e.get("category") in CORE_CATEGORIES
                    and e.get("metadata", {}).get("role") == role]

    def search(self, keyword: str) -> List[Dict[str, Any]]:
        """标题/内容关键词搜索。"""
        kw = (keyword or "").lower()
        if not kw:
            return []
        with self._lock:
            return [e for e in self._entries.values()
                    if kw in e["title"].lower() or kw in e["content"].lower()]

    def get_entries(self, category: str = "", tag: str = "") -> List[Dict[str, Any]]:
        """按分类/标签过滤条目（空参数不过滤）。"""
        with self._lock:
            entries = list(self._entries.values())
            if category:
                entries = [e for e in entries if e["category"] == category]
            if tag:
                entries = [e for e in entries if tag in e.get("tags", [])]
            return entries

    def get_entry(self, entry_id: str) -> Optional[Dict[str, Any]]:
        """获取单条。"""
        with self._lock:
            return self._entries.get(entry_id)

    def get_recent_updates(self, limit: int = 10) -> List[Dict[str, Any]]:
        """最近更新的条目。"""
        with self._lock:
            entries = sorted(self._entries.values(),
                             key=lambda e: e.get("updated_at", 0), reverse=True)
            return entries[:limit]

    def get_categories(self) -> Dict[str, List[str]]:
        """所有分类及其条目ID。"""
        with self._lock:
            return dict(self._categories)

    def system_prompt_block(self, role: str, max_chars: int = 1500) -> str:
        """生成注入 system_prompt 的核心设定块（角色世界观，控制 token）。

        格式：【世界设定】+ "- <title>：<content>" 逐行，按 core_entries(role)
        顺序累积，超 max_chars 截断。
        """
        entries = self.core_entries(role)
        if not entries:
            return ""
        lines = ["【世界设定】"]
        used = 0
        for e in entries:
            line = "- {}：{}".format(e["title"], e["content"])
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)

    # ================= CRUD（可写 + 落盘） =================

    def add_entry(self, entry_id: str, title: str, content: str,
                  category: str = "general",
                  tags: Optional[List[str]] = None,
                  metadata: Optional[Dict] = None) -> bool:
        """添加条目（id 冲突返回 False；新增自动标记落盘）。"""
        with self._lock:
            if entry_id in self._entries:
                return False
            now = time.time()
            self._entries[entry_id] = {
                "entry_id": entry_id, "title": title, "content": content,
                "category": category, "tags": tags or [],
                "metadata": metadata or {},
                "created_at": now, "updated_at": now, "version": 1,
            }
            self._categories.setdefault(category, []).append(entry_id)
            self._newly_added = True
            logger.info("[WorldBook] 添加条目: %s (%s)", title, category)
            return True

    def update_entry(self, entry_id: str, content: str = None,
                     title: str = None, tags: List[str] = None,
                     metadata: Optional[Dict] = None,
                     reason: str = "") -> bool:
        """更新条目（记录演化日志，截断 200 条）。

        metadata 非空时按键合并（如 {priority_note: true} 标记重要记事，
        任务五：用户审阅时决定，非自动）。
        """
        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None:
                return False
            old_content = entry["content"]
            if content is not None:
                entry["content"] = content
            if title is not None:
                entry["title"] = title
            if tags is not None:
                entry["tags"] = tags
            if metadata:
                entry["metadata"] = {**(entry.get("metadata") or {}), **metadata}
            entry["updated_at"] = time.time()
            entry["version"] += 1
            self._evolution_log.append({
                "entry_id": entry_id, "action": "update",
                "old_content": (old_content or "")[:100],
                "new_content": (content or old_content)[:100],
                "reason": reason, "timestamp": time.time(),
            })
            self._evolution_log = self._evolution_log[-200:]
            return True

    def remove_entry(self, entry_id: str) -> bool:
        """删除条目。"""
        with self._lock:
            entry = self._entries.pop(entry_id, None)
            if entry is None:
                return False
            cat = entry.get("category", "")
            if cat in self._categories:
                self._categories[cat] = [e for e in self._categories[cat]
                                         if e != entry_id]
            return True

    # ================= 自动进化（事件订阅，装配层启动） =================

    def start_evolving(self, event_bus, evolve_cfg: Optional[Dict[str, Any]] = None) -> None:
        """订阅弹幕/礼物事件自动进化世界书（幂等；配置缺失时用默认阈值）。"""
        if self._evolving:
            return
        if evolve_cfg:
            self._evolve_cfg.update(evolve_cfg)
        self._bus = event_bus
        try:
            event_bus.subscribe(DANMAKU_RECEIVED, self._on_danmaku, priority=20)
            event_bus.subscribe(GIFT_RECEIVED, self._on_gift, priority=20)
            self._evolving = True
            logger.info("[WorldBook] 自动进化已启动（订阅 danmaku/gift）")
        except Exception as e:
            logger.warning("[WorldBook] 自动进化订阅失败: %s", e)
            self._evolving = False

    def stop_evolving(self) -> None:
        """退订事件（幂等）。"""
        if not self._evolving or self._bus is None:
            return
        try:
            self._bus.unsubscribe(DANMAKU_RECEIVED, self._on_danmaku)
            self._bus.unsubscribe(GIFT_RECEIVED, self._on_gift)
        except Exception as e:
            logger.debug("[WorldBook] 退订失败: %s", e)
        self._evolving = False

    def _on_danmaku(self, event: str = "", **kw) -> None:
        """弹幕事件：分词统计话题频率 + 发言计数 → 阈值自动进化。"""
        text = str(kw.get("content") or kw.get("text") or "")
        user = str(kw.get("user_name") or "")
        uid = str(kw.get("user_id") or user or "")
        if not text or not self._evolve_cfg.get("enabled", True):
            return
        with self._lock:
            for word in self._tokenize(text):
                self._topic_freq[word] = self._topic_freq.get(word, 0) + 1
            if uid:
                rec = self._viewer_msg.setdefault(uid, {"name": user, "count": 0})
                rec["name"] = user or rec.get("name", "")
                rec["count"] += 1
            self._auto_evolve_locked()

    def _on_gift(self, event: str = "", **kw) -> None:
        """礼物事件：累计礼物价值（金瓜子=price×num）→ 阈值自动进化。"""
        user = str(kw.get("user_name") or "")
        extra = kw.get("extra") or {}
        num = int(extra.get("num", 1) or 1)
        price = int(extra.get("price", 0) or 0)
        gift = str(extra.get("gift_name") or kw.get("content") or "礼物")
        if not user or not self._evolve_cfg.get("enabled", True):
            return
        with self._lock:
            rec = self._viewer_gift.setdefault(user, {"value": 0, "gifts": {}})
            rec["value"] += price * num
            rec["gifts"][gift] = rec["gifts"].get(gift, 0) + num
            self._auto_evolve_locked()

    def _auto_evolve_locked(self) -> None:
        """阈值检查 → 生成条目（须持锁；新增条目后自动落盘）。"""
        try:
            threshold = int(self._evolve_cfg.get("topic_threshold", 10))
            for word, cnt in sorted(self._topic_freq.items(),
                                    key=lambda kv: kv[1], reverse=True):
                if cnt < threshold or word in STOP_WORDS:
                    continue
                eid = "wb_topic_" + str(abs(hash(word)) % 10 ** 8)
                if eid in self._entries:
                    continue
                self.add_entry(eid, f"观众话题：{word}",
                               f"最近直播中，观众频繁聊到「{word}」（累计 {cnt} 次）。"
                               f"这是角色和观众们的共同话题，可以自然地接住这个梗，"
                               f"或在冷场时主动提起。",
                               category="audience_insight",
                               tags=["话题", word])
            gift_th = int(self._evolve_cfg.get("viewer_gift_value", 100))
            for user, rec in self._viewer_gift.items():
                if rec["value"] < gift_th:
                    continue
                eid = "wb_viewer_gift_" + str(abs(hash(user)) % 10 ** 8)
                if eid in self._entries:
                    continue
                gift_desc = "、".join(f"{g}x{c}" for g, c in rec["gifts"].items())
                self.add_entry(eid, f"重要观众：{user}",
                               f"观众「{user}」是直播间的忠实支持者，累计送出礼物价值"
                               f" {rec['value']}（{gift_desc}）。遇到 TA 时要热情回应、"
                               f"优先照顾 TA 的弹幕。",
                               category="viewer", tags=["重要观众", user])
            msg_th = int(self._evolve_cfg.get("viewer_msg_count", 20))
            for uid, rec in self._viewer_msg.items():
                if rec["count"] < msg_th:
                    continue
                name = rec.get("name") or uid
                eid = "wb_viewer_msg_" + str(abs(hash(uid)) % 10 ** 8)
                if eid in self._entries:
                    continue
                self.add_entry(eid, f"常驻观众：{name}",
                               f"观众「{name}」经常来直播间互动（累计发言 {rec['count']} 条），"
                               f"是直播间的熟面孔，可以像老朋友一样打招呼。",
                               category="viewer", tags=["常驻观众", name])
            if self._newly_added:
                self._newly_added = False
                self.save_to_disk()
        except Exception as e:
            logger.warning("[WorldBook] 自动进化异常: %s", e)

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """分词（与旧系统一致）：英文整词；中文短句整取，长句 bigram。"""
        words = []
        for m in re.finditer(r"[\u4e00-\u9fff]+|[a-zA-Z]{2,}", text):
            seg = m.group()
            if seg and seg[0].isascii():
                words.append(seg)
            elif len(seg) <= 4:
                if seg not in STOP_WORDS:
                    words.append(seg)
            else:
                for i in range(len(seg) - 1):
                    w = seg[i:i + 2]
                    if w not in STOP_WORDS:
                        words.append(w)
        return words

    # ================= 增强：建议 / 手动进化 / 合并 =================

    def suggest(self, context: Dict[str, Any],
                max_suggestions: int = 5) -> List[Dict[str, Any]]:
        """基于上下文（事件/话题/角色）建议新的世界书条目或进化方向。"""
        suggestions: List[Dict[str, Any]] = []
        events = context.get("events", [])
        topics = context.get("topics", [])
        characters = context.get("characters", [])
        with self._lock:
            existing_titles = {e["title"] for e in self._entries.values()}
            existing_content = " ".join(e["content"] for e in self._entries.values())
        for event in events:
            event_text = event if isinstance(event, str) else event.get("text", "")
            if not event_text:
                continue
            related = any(t in event_text or event_text in t
                          for t in existing_titles)
            if not related and event_text not in existing_content:
                suggestions.append({"type": "new_entry", "title": event_text[:20],
                                    "reason": f"检测到未记录的事件: {event_text[:50]}",
                                    "suggested_category": "event", "priority": "medium"})
        for topic in topics:
            topic_text = topic if isinstance(topic, str) else topic.get("name", "")
            if not topic_text:
                continue
            mention_count = existing_content.count(topic_text)
            if mention_count == 0:
                suggestions.append({"type": "new_entry", "title": topic_text,
                                    "reason": f"话题'{topic_text}'尚无相关世界书条目",
                                    "suggested_category": "topic", "priority": "high"})
            elif mention_count < 2:
                suggestions.append({"type": "expand_entry", "title": topic_text,
                                    "reason": f"话题'{topic_text}'的设定内容较少，建议扩展",
                                    "suggested_category": "topic", "priority": "low"})
        for char in characters:
            char_name = char if isinstance(char, str) else char.get("name", "")
            if not char_name:
                continue
            if char_name not in existing_content:
                suggestions.append({"type": "new_entry", "title": f"角色: {char_name}",
                                    "reason": f"新角色'{char_name}'需要建立设定档案",
                                    "suggested_category": "character", "priority": "high"})
        now = time.time()
        with self._lock:
            stale = [e for e in self._entries.values()
                     if now - e.get("updated_at", 0) > 7 * 86400]
        for entry in stale[:3]:
            suggestions.append({"type": "update_entry", "entry_id": entry["entry_id"],
                                "title": entry["title"],
                                "reason": f"条目已{int((now - entry['updated_at']) / 86400)}天未更新，建议回顾",
                                "suggested_category": entry.get("category", "general"),
                                "priority": "low"})
        order = {"high": 0, "medium": 1, "low": 2}
        suggestions.sort(key=lambda s: order.get(s["priority"], 2))
        return suggestions[:max_suggestions]

    def evolve(self, event: Dict[str, Any]) -> List[str]:
        """根据事件手动进化：事件文本包含条目标题关键词时更新条目。返回被修改的条目ID。"""
        modified: List[str] = []
        event_type = event.get("type", "")
        event_text = event.get("text", "")
        with self._lock:
            for entry in self._entries.values():
                if entry["title"] in event_text:
                    entry["metadata"]["last_event"] = event_type
                    entry["updated_at"] = time.time()
                    entry["version"] += 1
                    modified.append(entry["entry_id"])
        if modified:
            self._version += 1
            self._evolution_log.append({"action": "evolve", "event": event_type,
                                        "modified_entries": modified,
                                        "timestamp": time.time()})
            logger.info("[WorldBook] 进化: %d 条目被修改", len(modified))
        return modified

    def merge_entries(self, entry_id_a: str, entry_id_b: str,
                      new_title: str = "") -> Optional[str]:
        """合并两个条目为一个新条目（原条目删除）。失败返回 None。"""
        with self._lock:
            ea = self._entries.get(entry_id_a)
            eb = self._entries.get(entry_id_b)
            if ea is None or eb is None:
                return None
        merged_content = f"{ea['content']}\n\n---\n\n{eb['content']}"
        merged_tags = list(set(ea.get("tags", []) + eb.get("tags", [])))
        new_id = f"merged_{entry_id_a}_{entry_id_b}"
        title = new_title or f"{ea['title']} & {eb['title']}"
        if self.add_entry(new_id, title, merged_content,
                          category=ea.get("category", "general"),
                          tags=merged_tags):
            self.remove_entry(entry_id_a)
            self.remove_entry(entry_id_b)
            with self._lock:
                self._evolution_log.append({"action": "merge",
                                            "source_entries": [entry_id_a, entry_id_b],
                                            "new_entry": new_id,
                                            "timestamp": time.time()})
            return new_id
        return None

    # ================= 导出 / 统计 =================

    def export_book(self) -> str:
        """导出世界书为 JSON 串（v2 格式）。"""
        with self._lock:
            book = {"version": self._version,
                    "entry_count": len(self._entries),
                    "entries": list(self._entries.values()),
                    "categories": dict(self._categories)}
            return json.dumps(book, ensure_ascii=False, indent=2)

    def get_evolution_log(self, limit: int = 20) -> List[Dict[str, Any]]:
        """演化日志（最近 limit 条）。"""
        with self._lock:
            return list(self._evolution_log[-limit:])

    def stats(self) -> Dict[str, Any]:
        """统计信息。"""
        with self._lock:
            return {"path": str(self._path), "version": self._version,
                    "total_entries": len(self._entries),
                    "evolving": self._evolving,
                    "evolution_events": len(self._evolution_log),
                    "categories": {k: len(v) for k, v in self._categories.items()}}


# 模块级默认实例（懒加载单例，供 commander 等多处复用）
_world_book: Optional[WorldBook] = None


def get_world_book() -> WorldBook:
    """获取模块级世界书实例（懒加载）。"""
    global _world_book
    if _world_book is None:
        _world_book = WorldBook()
    return _world_book
