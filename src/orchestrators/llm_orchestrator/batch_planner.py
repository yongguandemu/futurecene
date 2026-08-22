"""batch_planner.py — 批量发言计划（任务二，LLM 调度官子模块）

主动发言按「批」预生成：一次 LLM 调用产出多条发言计划（几分钟窗口），
降低调用频次与延迟。每条计划 = {text, mood, suggested_window_sec, duration_estimate}。

- 结构化输出：提示词要求 JSON 数组，容错解析（```json 块 / 截取 []）
- 校验钳制：mood 白名单、window/duration 数值范围钳制、text 经 TTS Preprocessor 清洗
- 降级链：LLM 失败/解析 0 条 → 素材话题池单条回退（保持有输出）

# 模块内容清单 — batch_planner

## 1. 模块身份标识
- 所属调度官：llm · batch_planner · 能力 llm:batch_plan（内部子能力）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| count | 否 | 3 | int [1,8] | 单批计划条数 |
| max_len | 否 | 120 | int | 单条文本最大长度 |

## 3. 输入契约
- generate(context: str, role: str, topics: list[str], count: int) -> list[dict]

## 4. 输出契约
- 成功：list[{text, mood, suggested_window_sec, duration_estimate}]（≥1 条，失败兜底 1 条）
- 失败：无（永不抛错，全链失败返回话题池兜底）

## 5. 依赖声明
- 外部服务：LLM（chat_fn 注入，None 时纯话题池）
- 内部模块：tts_preprocessor（清洗）；active_dialogue.DEFAULT_TOPICS（兜底素材）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| LLM 调用异常 | 模型不可用/超时 | 回退话题池单条 |
| JSON 解析失败 | 模型输出非 JSON | 截取 [] 块重试，仍失败回退 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | - | 无状态 |

## 8. 领域状态说明
- 状态项：无
- 持久化：无
"""
import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

VALID_MOODS = {"happy", "curious", "shy", "calm", "sad", "angry", "surprised", "default"}

_PROMPT_TEMPLATE = """你是虚拟主播的发言策划。基于以下上下文，生成 {count} 条适合主动开口的发言计划（JSON 数组，不要输出其他内容）：
上下文：{context}
角色：{role}
可参考素材：{topics}

输出格式：
[{{"text": "发言文本", "mood": "happy|curious|shy|calm|sad|angry|surprised|default",
   "suggested_window_sec": 数字（建议发言时间窗秒数，30-300）,
   "duration_estimate": 数字（预计朗读秒数，3-30）}}]
要求：发言口语化、自然、贴合角色人设；不要输出 JSON 以外的解释文字。"""

_JSON_BLOCK_RE = re.compile(r"\[[\s\S]*\]")


class BatchPlanner:
    """LLM 批量生成发言计划；失败降级话题池单条。"""

    def __init__(self, chat_fn: Optional[Callable[[List[Dict[str, str]]], Any]] = None,
                 preprocessor=None, max_len: int = 120) -> None:
        self._chat_fn = chat_fn  # fn(messages) -> (reply, usage)；None 时纯话题池
        self._preprocessor = preprocessor
        self._max_len = max(20, int(max_len))

    def generate(self, context: str = "", role: str = "", topics: Optional[List[str]] = None,
                 count: int = 3) -> List[Dict[str, Any]]:
        count = max(1, min(8, int(count)))
        topics = [t for t in (topics or []) if str(t).strip()]
        plans = self._llm_plans(context or "", role or "", topics, count)
        if plans:
            return plans
        return self._fallback_plans(topics, count)

    # ---------- LLM 路径 ----------

    def _llm_plans(self, context: str, role: str, topics: List[str], count: int) -> List[Dict[str, Any]]:
        if self._chat_fn is None:
            return []
        prompt = _PROMPT_TEMPLATE.format(
            count=count, context=context[:500], role=role or "虚拟主播",
            topics="；".join(topics[:8]) if topics else "（无）")
        messages = [{"role": "system", "content": "你是严谨的结构化输出助手，只输出 JSON。"},
                    {"role": "user", "content": prompt}]
        try:
            reply, _usage = self._chat_fn(messages)
            parsed = self._parse_json(reply or "")
            return self._validate(parsed, count)
        except Exception as e:
            logger.warning("[BatchPlanner] LLM 批量生成失败，话题池兜底: %s", e)
            return []

    def _parse_json(self, reply: str) -> Optional[List[Any]]:
        text = reply.strip()
        # 容错：优先提取 ```json ... ``` 块，其次整个文本中的首个 [...] 块
        for block in re.findall(r"```(?:json)?\s*([\s\S]*?)```", text):
            try:
                return json.loads(block.strip())
            except Exception:
                continue
        m = _JSON_BLOCK_RE.search(text)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        try:
            return json.loads(text)
        except Exception:
            return None

    def _validate(self, parsed: Optional[List[Any]], count: int) -> List[Dict[str, Any]]:
        if not isinstance(parsed, list):
            return []
        plans: List[Dict[str, Any]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            text = self._clean_text(str(item.get("text", "")))
            if not text:
                continue
            mood = str(item.get("mood", "default")).strip().lower()
            if mood not in VALID_MOODS:
                mood = "default"
            try:
                window = float(item.get("suggested_window_sec", 120))
            except (TypeError, ValueError):
                window = 120.0
            try:
                duration = float(item.get("duration_estimate", 10))
            except (TypeError, ValueError):
                duration = 10.0
            plans.append({
                "text": text,
                "mood": mood,
                "suggested_window_sec": round(max(30.0, min(300.0, window))),
                "duration_estimate": round(max(3.0, min(60.0, duration)), 1),
            })
            if len(plans) >= count:
                break
        return plans

    # ---------- 降级路径 ----------

    def _fallback_plans(self, topics: List[str], count: int) -> List[Dict[str, Any]]:
        """话题池兜底：优先用素材 topics，其次内置 DEFAULT_TOPICS。"""
        from src.orchestrators.llm_orchestrator.active_dialogue import DEFAULT_TOPICS
        pool = topics or [t["text"] for t in DEFAULT_TOPICS]
        plans = []
        for i in range(count):
            text = self._clean_text(pool[i % len(pool)] if pool else "嗯…说点什么好呢？")
            if not text:
                continue
            plans.append({"text": text, "mood": "default",
                          "suggested_window_sec": 120, "duration_estimate": 8.0})
        return plans

    def _clean_text(self, text: str) -> str:
        if self._preprocessor is not None:
            try:
                return self._preprocessor.clean(text)
            except Exception:
                pass
        return str(text).strip()[: self._max_len]
