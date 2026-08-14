"""cost_tracker.py — API 成本追踪（P5，规格书 1065 行，旧项目 api_cost_tracker.py 模式）

按调用类型（llm/tts）与模型累计 token/费用；计价表可按实际账单调整。

# 模块内容清单（8 项契约）
1. 模块身份标识：commander · CostTracker · 对外 record/snapshot/reset
2. 配置契约：PRICING 计价表 + TTS_PRICE_PER_CHAR；构造参数 persist/persist_path/event_bus；持久化 data/cost.json
3. 输入契约：record(call_type, provider, model, prompt_tokens, completion_tokens, chars)
4. 输出契约：record 返回本次费用（USD）；snapshot 返回 by_type/total_cost/total_calls；累计每跨整 1.00 发布 COST_MILESTONE；可选持久化 JSON
5. 依赖声明：json、logging、threading、time、pathlib、typing、shared.config_loader、shared.events
6. 错误定义：计价表缺失模型回退 gpt-4o-mini 单价；加载损坏 JSON 静默忽略；里程碑发布异常静默
7. 生命周期方法：record()/snapshot()/reset()
8. 领域状态说明：_by_type 分类型累计、_total_cost/_total_calls、_path 持久化路径、_event_bus 可选事件总线
"""
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from src.shared.config_loader import PROJECT_ROOT

logger = logging.getLogger(__name__)

# 计价表：USD / 1K token（Chat 类按输入/输出区分，此处简化为统一单价）
# TODO: 确认 — 按实际订阅账单校准单价
# DeepSeek V4 Pro 官方价（2026-08-14）：输入 3 元/百万 token，输出 6 元/百万 token
# 换算：1 元 ≈ 0.14 USD；输入 3 元/百万 = 0.00042 USD/1K，输出 6 元/百万 = 0.00084 USD/1K
PRICING: Dict[str, Dict[str, float]] = {
    "deepseek-v4-pro": {"input": 0.00042, "output": 0.00084},
    "gpt-4o-mini": {"input": 0.0006, "output": 0.0006},
    "gpt-4o": {"input": 0.005, "output": 0.005},
    "glm-4.7-flashx": {"input": 0.0001, "output": 0.0001},
    "glm-4.7-flash": {"input": 0.0001, "output": 0.0001},
    "glm-4-plus": {"input": 0.005, "output": 0.005},
}
TTS_PRICE_PER_CHAR = 0.00005  # USD / 字符（估算）

COST_FILE = PROJECT_ROOT / "data" / "cost.json"


class CostTracker:
    """成本追踪（线程安全）。"""

    def __init__(self, persist: bool = True, persist_path: str = "",
                 event_bus=None):
        self._persist = persist
        self._path = Path(persist_path) if persist_path else COST_FILE
        self._lock = threading.RLock()
        self._by_type: Dict[str, Dict[str, Any]] = {}  # {llm/tts: {cost, calls, tokens}}
        self._total_cost = 0.0
        self._total_calls = 0
        self._event_bus = event_bus  # 可选注入，跨整元发布 cost:milestone
        self._load()

    def record(self, call_type: str, provider: str = "", model: str = "",
               prompt_tokens: int = 0, completion_tokens: int = 0,
               chars: int = 0) -> float:
        """记录一次调用并返回本次费用（USD）。"""
        if call_type == "tts":
            cost = chars * TTS_PRICE_PER_CHAR
        else:
            price = PRICING.get(model, PRICING.get("gpt-4o-mini", {}))
            if isinstance(price, dict):
                input_unit = price.get("input", 0.0006)
                output_unit = price.get("output", 0.0006)
                cost = (prompt_tokens / 1000.0 * input_unit
                        + completion_tokens / 1000.0 * output_unit)
            else:  # 兼容旧 float 计价
                cost = (prompt_tokens + completion_tokens) / 1000.0 * price
        with self._lock:
            item = self._by_type.setdefault(call_type, {"cost": 0.0, "calls": 0, "tokens": 0})
            item["cost"] += cost
            item["calls"] += 1
            item["tokens"] += prompt_tokens + completion_tokens
            self._total_cost += cost
            self._total_calls += 1
            # 跨整元里程碑（每累计满 1.00 发布 cost:milestone，触发 state:changed）
            crossed_milestone = int(self._total_cost) > int(self._total_cost - cost)
            if self._persist:
                self._save()
        if crossed_milestone and self._event_bus is not None:
            try:
                from src.shared.events import COST_MILESTONE
                self._event_bus.publish(COST_MILESTONE, total_cost=self._total_cost)
            except Exception:
                pass
        return cost

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"by_type": {k: dict(v) for k, v in self._by_type.items()},
                    "total_cost": self._total_cost,
                    "total_calls": self._total_calls}

    def reset(self) -> None:
        with self._lock:
            self._by_type.clear()
            self._total_cost = 0.0
            self._total_calls = 0
            if self._persist:
                self._save()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._by_type = data.get("by_type", {})
            self._total_cost = data.get("total_cost", 0.0)
            self._total_calls = data.get("total_calls", 0)
        except (json.JSONDecodeError, OSError):
            pass

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2),
                              encoding="utf-8")
