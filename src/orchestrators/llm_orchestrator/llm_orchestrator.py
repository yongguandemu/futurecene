"""llm_orchestrator.py — LLM 调度官主类（规格书 5.4）

职责边界（ADR-005）：只做 LLM API 调用封装（流式、重试、降级、成本统计）。
不包含：人格组装（system_prompt 由指挥官注入）、记忆注入、意图路由。

引擎路由（成本控制，ADR-006 实测修正）：payload.engine 决定降级链——
- "fast"（日常对话/弹幕/主动话题）：openai(DeepSeek V4 Flash) → zhipu → 本地兜底
- "pro"/缺省（复杂 Agent：游戏规划等）：openai(DeepSeek V4 Pro) → zhipu → 本地兜底
缺省 pro 保证向后兼容与安全（贵但不会错）；对话调用方显式传 fast 声明低延迟意图。
全链失败发布 llm:failed。

# 模块内容清单（8 项契约）
1. 模块身份标识：llm 调度官 · llm_orchestrator · 能力 llm:chat / llm:stream_chat / llm:active_dialogue
2. 配置契约：llm.openai（api_key/base_url/model/timeout）、llm.zhipu（api_key/model/timeout）、llm.active_dialogue（min_interval 等），回退 os.environ
3. 输入契约：handle(command) 接收 {"capability": "llm:*", "payload": {"system_prompt","history","text","engine"}}；engine ∈ "fast"/"pro"（缺省 pro）；构造注入 event_bus / clients / config_loader
4. 输出契约：返回 {"ok": true, "data": {"reply","usage","latency_ms"}, "error": str|null}；发布 LLM_REQUESTED / LLM_RESPONDED / LLM_STREAM_CHUNK / LLM_FAILED
5. 依赖声明：registry、active_dialogue、glm_client、openai_client、src.shared.events
6. 错误定义：首选引擎调用异常 → 降级下一引擎（记 warning）；全链失败 → 本地兜底回复 + 发布 llm:failed
7. 生命周期方法：start() 初始化主/备客户端、stop()、health()、handle() 能力分发
8. 领域状态说明：_primary/_fallback（客户端）、_started、_last_error、_active（主动对话引擎）
"""
import concurrent.futures
import logging
import os
import time
from typing import Any, Dict, List, Optional

from src.orchestrators.llm_orchestrator import registry
from src.orchestrators.llm_orchestrator.active_dialogue import ActiveDialogue
from src.orchestrators.llm_orchestrator.glm_client import GLMClient
from src.orchestrators.llm_orchestrator.openai_client import OpenAIClient
from src.shared.events import LLM_FAILED, LLM_REQUESTED, LLM_RESPONDED, LLM_STREAM_CHUNK

logger = logging.getLogger(__name__)

# LLM 调用线程池（线程级超时用；挂起线程在网络恢复后自然结束）
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm-call")

_FALLBACK_REPLY = "我这边网络有点状况，先稍等一下，马上回来~"

# 引擎路由模式（成本控制）：fast=日常对话(GLM-FlashX 优先)，pro=复杂 Agent(DeepSeek 优先)
ROUTE_FAST = "fast"
ROUTE_PRO = "pro"


class LLMOrchestrator:
    """LLM 调度官：llm:chat / llm:stream_chat"""

    name = "llm"

    def __init__(self, event_bus, clients: Optional[Dict[str, Any]] = None,
                 config_loader=None):
        self._event_bus = event_bus
        self._clients = clients or {}  # {"openai": client, "zhipu": client}（测试注入）
        self._config_loader = config_loader  # ConfigLoader（密钥从 config.yaml 占位符解析）
        self._primary: Optional[Any] = None
        self._primary_fast: Optional[Any] = None  # fast 引擎专用（DeepSeek V4 Flash）
        self._fallback: Optional[Any] = None
        self._started = False
        self._last_error: Optional[str] = None
        # 主动对话引擎（冷场救星）
        self._active = ActiveDialogue(event_bus=event_bus, config=self._active_config())
        self._active.set_generator(self._generate_active_topic)
        registry.bind(self.handle)

    # ---------- OrchestratorProtocol ----------

    def capabilities(self) -> List[str]:
        return registry.capabilities()  # 从 registry 派生

    def start(self) -> None:
        if self._started:
            return
        # 配置契约：优先 ConfigLoader（config.yaml 占位符 → 环境变量），回退 os.environ
        llm_cfg = self._config_loader.get("llm", {}) if self._config_loader else {}
        openai_cfg = llm_cfg.get("openai", {}) or {}
        zhipu_cfg = llm_cfg.get("zhipu", {}) or {}
        self._primary = self._clients.get("openai") or OpenAIClient(
            api_key=openai_cfg.get("api_key") or os.environ.get("OPENAI_API_KEY", ""),
            base_url=openai_cfg.get("base_url")
            or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1/"),
            model=openai_cfg.get("model") or os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            timeout=float(os.environ.get("OPENAI_TIMEOUT", "12")),
        )
        # fast 引擎专用客户端：DeepSeek V4 Flash（免费且快，日常对话/弹幕）。
        # 实测 zhipu 在网络不佳时 12s+ 才返回且必超时，而 flash 8s 内返回，故 fast 改用
        # DeepSeek V4 Flash 优先，zhipu 降级兜底（config llm.openai.model_fast / OPENAI_MODEL_FAST）。
        fast_model = (openai_cfg.get("model_fast")
                      or os.environ.get("OPENAI_MODEL_FAST", ""))
        self._primary_fast = None
        if fast_model:
            self._primary_fast = OpenAIClient(
                api_key=openai_cfg.get("api_key") or os.environ.get("OPENAI_API_KEY", ""),
                base_url=openai_cfg.get("base_url")
                or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1/"),
                model=fast_model,
                timeout=float(os.environ.get("OPENAI_TIMEOUT", "12")),
            )
        self._fallback = self._clients.get("zhipu") or GLMClient(
            api_key=zhipu_cfg.get("api_key") or os.environ.get("ZHIPU_API_KEY", ""),
            model=zhipu_cfg.get("model") or os.environ.get("ZHIPU_MODEL", "glm-4.7-flash"),
            # config zhipu.timeout 优先（当前 6s），回退环境变量/默认 8s：
            # 修复超时配置不生效导致 zhipu 白等重试、降级叠加 20s+ 的慢响应
            timeout=float(zhipu_cfg.get("timeout")
                          or os.environ.get("ZHIPU_TIMEOUT", "8")),
            # 降级链本身即兜底：zhipu 失败立即降级 openai，不再内部重试叠加延迟
            # （旧默认 max_retries=1 会在超时后再重试一次，单引擎白等 16s+）
            max_retries=int(zhipu_cfg.get("max_retries", 0)),
        )
        # 线程级调用超时（config.yaml llm.<engine>.timeout 优先，回退环境变量/默认）
        self._timeouts = {
            "zhipu": float(zhipu_cfg.get("timeout")
                           or os.environ.get("ZHIPU_TIMEOUT", "10")),
            "openai": float(openai_cfg.get("timeout")
                            or os.environ.get("OPENAI_TIMEOUT", "15")),
        }
        self._started = True
        logger.info("[LLMOrchestrator] 已启动：primary=openai(pro=%s) fast=%s fallback=zhipu "
                    "timeouts=%s",
                    getattr(self._primary, "model", "?"),
                    fast_model or "none", self._timeouts)

    async def handle(self, command: Dict[str, Any]) -> Dict[str, Any]:
        capability = command.get("capability", "")
        payload = command.get("payload", {})
        if capability == "llm:chat":
            return self._chat(payload)
        if capability == "llm:stream_chat":
            return self._stream_chat(payload)
        if capability == "llm:active_dialogue":
            return self._active_dialogue(payload)
        if capability == "llm:batch_plan":
            return self._batch_plan(payload)
        return {"ok": False, "data": {}, "error": f"unknown capability: {capability}"}

    def health(self) -> Dict[str, Any]:
        if not self._started:
            return {"status": "down", "detail": "not started"}
        return {"status": "ok", "detail": self._last_error or "primary=openai fallback=zhipu"}

    def stop(self) -> None:
        self._started = False

    # ---------- 内部实现 ----------

    @staticmethod
    def _call_with_timeout(fn, timeout: float, name: str):
        """线程级超时调用：SDK 自带超时不可靠（智谱实测 8s 配置可挂 77s）时兜底。

        超时后立即抛 TimeoutError 让上层走降级链；挂起的线程会在网络恢复后自然结束，
        演示/生产场景均可接受（每次超时最多泄漏一个正在等待的 SDK 调用线程）。
        """
        fut = _EXECUTOR.submit(fn)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            logger.warning("[LLMOrchestrator] %s 调用线程级超时（%.0fs），触发降级", name, timeout)
            raise TimeoutError(f"{name} 调用超时 ({timeout}s)")
        except Exception as e:
            raise e

    def _build_messages(self, payload: Dict[str, Any]) -> List[Dict[str, str]]:
        """组装 messages：system_prompt（指挥官注入）→ history → 当前用户输入。"""
        system_prompt = payload.get("system_prompt") or ""
        history = payload.get("history") or []
        text = payload.get("text", "")
        messages: List[Dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(history)
        messages.append({"role": "user", "content": text})
        return messages

    def _engine_chain(self, engine: str):
        """按路由模式返回 (client, engine_name) 降级链。

        fast：openai(DeepSeek V4 Flash) 优先 → zhipu 兜底；
        pro/其他：openai(DeepSeek V4 Pro) 优先 → zhipu 兜底。
        （实测调优：zhipu 网络不佳时必超时，故 fast 也以 DeepSeek Flash 为主引擎）
        """
        if engine == ROUTE_FAST:
            return ((self._primary_fast or self._primary, "openai"),
                    (self._fallback, "zhipu"))
        return ((self._primary, "openai"), (self._fallback, "zhipu"))

    def _chat(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._event_bus.publish(LLM_REQUESTED, capability="llm:chat", text=payload.get("text", ""))
        messages = self._build_messages(payload)
        engine = payload.get("engine", ROUTE_PRO)
        chain = self._engine_chain(engine)
        first_name = chain[0][1]
        for client, name in chain:
            if client is None:
                continue
            try:
                t0 = time.time()
                reply, usage = client.chat(messages)
                latency_ms = int((time.time() - t0) * 1000)
                if name != first_name:
                    logger.warning("[LLMOrchestrator] 降级: %s 失败 → %s (engine=%s)",
                                   first_name, name, engine)
                self._event_bus.publish(LLM_RESPONDED, capability="llm:chat", text=reply)
                return {"ok": True,
                        "data": {"reply": reply, "usage": usage or {}, "latency_ms": latency_ms},
                        "error": None}
            except Exception as e:
                self._last_error = str(e)
                logger.error("[LLMOrchestrator] %s 调用失败 (engine=%s): %s", name, engine, e)
        # 全链失败：本地兜底回复 + 发布 llm:failed（规格书 958 行）
        self._event_bus.publish(LLM_FAILED, capability="llm:chat", error=self._last_error)
        return {"ok": True,
                "data": {"reply": _FALLBACK_REPLY, "usage": {}, "latency_ms": 0},
                "error": self._last_error}

    def _stream_chat(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._event_bus.publish(LLM_REQUESTED, capability="llm:stream_chat",
                                text=payload.get("text", ""))
        messages = self._build_messages(payload)
        engine = payload.get("engine", ROUTE_PRO)
        chain = self._engine_chain(engine)
        first_name = chain[0][1]
        for client, name in chain:
            if client is None:
                continue
            try:
                t0 = time.time()
                chunks: List[str] = []
                for chunk in client.stream_chat(messages):
                    self._event_bus.publish(LLM_STREAM_CHUNK, capability="llm:stream_chat",
                                            chunk=chunk)
                    chunks.append(chunk)
                reply = "".join(chunks)
                latency_ms = int((time.time() - t0) * 1000)
                if name != first_name:
                    logger.warning("[LLMOrchestrator] 流式降级: %s 失败 → %s (engine=%s)",
                                   first_name, name, engine)
                self._event_bus.publish(LLM_RESPONDED, capability="llm:stream_chat", text=reply)
                return {"ok": True,
                        "data": {"reply": reply, "usage": {}, "latency_ms": latency_ms},
                        "error": None}
            except Exception as e:
                self._last_error = str(e)
                logger.error("[LLMOrchestrator] %s 流式调用失败 (engine=%s): %s", name, engine, e)
        # 全链失败：本地兜底回复 + 发布 llm:failed
        self._event_bus.publish(LLM_FAILED, capability="llm:stream_chat", error=self._last_error)
        return {"ok": True,
                "data": {"reply": _FALLBACK_REPLY, "usage": {}, "latency_ms": 0},
                "error": self._last_error}

    # ---------- 主动对话 ----------

    def _active_config(self) -> dict:
        """读取配置中的主动对话参数（缺省用引擎默认值）。"""
        if self._config_loader:
            try:
                return self._config_loader.get("llm", {}).get("active_dialogue", {}) or {}
            except Exception:
                return {}
        return {}

    def _generate_active_topic(self) -> str:
        """主动话题生成：走真实 LLM chat（fast 引擎，GLM-FlashX 优先），失败返回空串由话题池兜底。"""
        try:
            resp = self._chat({"text": "主动和观众打个招呼，聊点轻松的话题。",
                               "system_prompt": "", "history": [],
                               "engine": ROUTE_FAST})
            return resp.get("data", {}).get("reply", "") or ""
        except Exception:
            return ""

    def _active_dialogue(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """主动对话能力路由：tick / start / stop / status。"""
        action = payload.get("action", "tick")
        if action == "start":
            self._active.start()
            return {"ok": True, "data": {"running": True}, "error": None}
        if action == "stop":
            self._active.stop()
            return {"ok": True, "data": {"running": False}, "error": None}
        if action == "status":
            return {"ok": True, "data": self._active.get_status(), "error": None}
        result = self._active.tick()
        return {"ok": True, "data": {"triggered": bool(result), "result": result},
                "error": None}

    def _batch_chat(self, messages: List[Dict[str, str]]):
        """BatchPlanner 的 LLM 通道：fast 引擎（DeepSeek V4 Flash）优先，zhipu 兜底。"""
        chain = self._engine_chain(ROUTE_FAST)
        last_err: Optional[Exception] = None
        for client, _name in chain:
            if client is None:
                continue
            try:
                return client.chat(messages)
            except Exception as e:
                last_err = e
        raise RuntimeError(f"batch chat all engines failed: {last_err}")

    def _batch_plan(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """批量发言计划能力：llm:batch_plan {context, role, topics, count}。"""
        plans = self._planner.generate(
            context=payload.get("context", ""),
            role=payload.get("role", ""),
            topics=payload.get("topics") or [],
            count=int(payload.get("count", 3)),
        )
        return {"ok": True, "data": {"plans": plans, "count": len(plans)}, "error": None}
