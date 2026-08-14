"""openai_client.py — OpenAI API 调用封装（规格书 5.4 llm:chat 主引擎）

- chat：非流式对话，失败自动重试（指数退避，默认 2 次）
- stream_chat：流式对话，逐 chunk 产出文本增量

# 模块内容清单（8 项契约）
1. 模块身份标识：llm 调度官 · openai_client · 能力 llm:chat 主引擎（openai）
2. 配置契约：api_key / base_url(https://api.openai.com/v1/) / model(gpt-4o-mini) / timeout(12.0) / max_retries(2)
3. 输入契约：chat(messages, temperature, max_tokens) / stream_chat(messages, temperature)
4. 输出契约：chat 返回 (reply, usage)；stream_chat 逐 chunk 产出文本增量
5. 依赖声明：openai（OpenAI，可选，缺失时构造抛 RuntimeError）
6. 错误定义：openai 未安装抛 RuntimeError；调用失败重试后抛最后异常
7. 生命周期方法：无（构造即建客户端）
8. 领域状态说明：_client（OpenAI 实例）、model、max_retries
"""
import logging
import time
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)

try:
    from openai import OpenAI
except ImportError:  # 未安装时由构造函数抛出明确错误
    OpenAI = None  # type: ignore


class OpenAIClient:
    """OpenAI 兼容 API 客户端（主引擎）。"""

    engine_name = "openai"

    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1/",
                 model: str = "gpt-4o-mini", timeout: float = 12.0, max_retries: int = 2):
        if OpenAI is None:
            raise RuntimeError("openai 库未安装，请执行 pip install openai")
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        self.model = model
        self.max_retries = max_retries

    def chat(self, messages: List[Dict[str, str]], temperature: float = 0.9,
             max_tokens: Optional[int] = None):
        """非流式对话。失败自动重试（指数退避），返回 (reply, usage)。"""
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.chat.completions.create(
                    model=self.model, messages=messages,
                    temperature=temperature, max_tokens=max_tokens,
                )
                reply = (resp.choices[0].message.content or "").strip()
                return reply, self._extract_usage(resp)
            except Exception as e:
                last_error = e
                logger.warning("[OpenAIClient] 调用失败 (attempt=%d/%d): %s",
                               attempt + 1, self.max_retries + 1, e)
                if attempt < self.max_retries:
                    time.sleep(0.5 * (2 ** attempt))
        assert last_error is not None
        raise last_error

    def stream_chat(self, messages: List[Dict[str, str]],
                    temperature: float = 0.9) -> Generator[str, None, None]:
        """流式对话，逐 chunk 产出文本增量。"""
        stream = self._client.chat.completions.create(
            model=self.model, messages=messages, temperature=temperature, stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content

    @staticmethod
    def _extract_usage(resp: Any) -> Dict[str, int]:
        usage = getattr(resp, "usage", None)
        if usage is None:
            return {}
        return {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0),
            "completion_tokens": getattr(usage, "completion_tokens", 0),
            "total_tokens": getattr(usage, "total_tokens", 0),
        }
