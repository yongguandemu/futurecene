"""protocols.py — 调度官契约协议（规格书 4.3）

所有分 brain 必须实现 OrchestratorProtocol，这是两级扩展机制成立的前提。
总 brain 只认能力名，不关心实现细节。

# 模块内容清单（8 项契约）
1. 模块身份标识：commander · OrchestratorProtocol · 对外协议定义（总 brain 与分 brain 的契约）
2. 配置契约：无配置；定义能力声明/统一入口/生命周期契约
3. 输入契约：handle(command) 统一入口 {capability, payload}
4. 输出契约：handle 返回 {ok, data, error}；health 返回 {status, detail}
5. 依赖声明：typing.Protocol
6. 错误定义：协议约束（分 brain 未实现即违反契约）
7. 生命周期方法：start()/stop()/health()
8. 领域状态说明：name 唯一标识 + capabilities() 能力声明
"""
from typing import Any, Dict, List, Protocol


class OrchestratorProtocol(Protocol):
    """所有分 brain 必须实现的契约。"""

    name: str  # 分 brain 唯一标识，如 "tts"

    def capabilities(self) -> List[str]:
        """能力声明，如 ["tts:synthesize", "tts:stop"]。"""

    async def handle(self, command: Dict[str, Any]) -> Dict[str, Any]:
        """统一入口：command = {"capability": str, "payload": dict}
        返回 {"ok": bool, "data": dict, "error": str | None}"""

    def start(self) -> None:
        """注册时由 Commander 调用；按注册顺序执行。

        # TODO: 确认 — 规格书 4.3 标注 async，但 4.2 注册表为同步调用，此处按调用方实现为同步。
        """

    def stop(self) -> None:
        """注销/关闭时调用；逆序执行。

        # TODO: 确认 — 同上，规格书 4.3 标注 async，按 4.2 同步调用实现。
        """

    def health(self) -> Dict[str, Any]:
        """健康检查，返回 {"status": "ok"|"degraded"|"down", "detail": str}"""
