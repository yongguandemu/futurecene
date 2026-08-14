"""safety_orchestrator.py — 安全调度官主类（规格书 5.4）

能力：safety:check_input / check_output / reload_rules。
职责边界（5.5）：不决定回复策略，只返回 verdict（allow/block/flag），策略归指挥官。

# 模块内容清单（8 项契约）
1. 模块身份标识：safety · SafetyOrchestrator · 能力 safety:check_input/check_output/reload_rules
2. 配置契约：rules_file 规则文件路径、model_dir 模型目录（可选）
3. 输入契约：handle(command) 指令字典（capability + payload.text）
4. 输出契约：{ok, data:{verdict, reason}, error}；发布 safety:blocked / safety:flagged 事件
5. 依赖声明：logging、typing、registry、KeywordFilter、ModelFilter、src.shared.events
6. 错误定义：未知 capability 返回 error；空文本返回 allow
7. 生命周期方法：start()/stop()/health()
8. 领域状态说明：_started 启动标记；_keyword 规则过滤器、_model 模型过滤器实例
"""
import logging
from typing import Any, Dict, List

from src.orchestrators.safety_orchestrator import registry
from src.orchestrators.safety_orchestrator.keyword_filter import KeywordFilter
from src.orchestrators.safety_orchestrator.model_filter import ModelFilter
from src.shared.events import SAFETY_BLOCKED, SAFETY_FLAGGED

logger = logging.getLogger(__name__)


class SafetyOrchestrator:
    """安全调度官。"""

    name = "safety"

    def __init__(self, event_bus, rules_file: str = "", model_dir: str = ""):
        self._event_bus = event_bus
        self._keyword = KeywordFilter(rules_file=rules_file)
        self._model = ModelFilter(model_dir=model_dir)
        self._started = False
        registry.bind(self.handle)

    def capabilities(self) -> List[str]:
        return registry.capabilities()

    def start(self) -> None:
        self._started = True
        logger.info("[SafetyOrchestrator] 已启动（模型推理=%s）", self._model.available)

    async def handle(self, command: Dict[str, Any]) -> Dict[str, Any]:
        capability = command.get("capability", "")
        payload = command.get("payload", {})
        if capability in ("safety:check_input", "safety:check_output"):
            return self._check(payload)
        if capability == "safety:reload_rules":
            return self._reload_rules()
        return {"ok": False, "data": {}, "error": f"unknown capability: {capability}"}

    def health(self) -> Dict[str, Any]:
        return {"status": "ok" if self._started else "down",
                "detail": f"model_filter={self._model.available}"}

    def stop(self) -> None:
        self._started = False

    # ---------- 内部实现 ----------

    def _check(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        text = payload.get("text", "")
        if not text:
            return {"ok": True, "data": {"verdict": "allow", "reason": "empty"},
                    "error": None}
        verdict, matched = self._keyword.check(text)
        if verdict == "block":
            self._event_bus.publish(SAFETY_BLOCKED, text=text, matched=matched)
        elif verdict == "flag":
            self._event_bus.publish(SAFETY_FLAGGED, text=text, matched=matched)
        reason = f"命中敏感词: {matched}" if verdict != "allow" else ""
        return {"ok": True,
                "data": {"verdict": verdict, "reason": reason},
                "error": None}

    def _reload_rules(self) -> Dict[str, Any]:
        count = self._keyword.reload()
        return {"ok": True, "data": {"loaded": count}, "error": None}
