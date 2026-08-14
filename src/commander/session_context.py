"""session_context.py — 会话状态（规格书 4.6，状态归属规则 ADR-001）

会话状态（角色/场景/直播模式）归指挥官；领域内部状态归各调度官。

# 模块内容清单（8 项契约）
1. 模块身份标识：commander · SessionContext · 对外 switch_role/switch_scene/snapshot/bind_event_bus
2. 配置契约：VALID_ROLES/VALID_SCENES 常量约束合法取值
3. 输入契约：switch_role(role)/switch_scene(scene)
4. 输出契约：切换返回 bool；snapshot 返回会话状态字典；发布 SESSION_SWITCHED 事件
5. 依赖声明：time、dataclasses、typing、shared.events
6. 错误定义：非法角色/场景返回 False（不切换）
7. 生命周期方法：bind_event_bus()（装配时绑定 EventBus）
8. 领域状态说明：role/scene/live_mode/started_at（会话状态归指挥官，ADR-001）
"""
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from src.shared.events import SESSION_SWITCHED

VALID_ROLES = {"yuki", "lilith"}
VALID_SCENES = {"chat", "game", "vn", "mc"}


@dataclass
class SessionContext:
    """会话上下文（指挥官持有，唯一状态源）。"""
    session_id: str
    role: str = "yuki"  # 当前角色
    scene: str = "chat"  # chat / game / vn / mc
    live_mode: str = "offline"  # offline / live
    started_at: float = field(default_factory=time.time)

    def __post_init__(self):
        self._event_bus: Optional[Any] = None

    def bind_event_bus(self, event_bus) -> None:
        """装配时绑定 EventBus（用于状态变更事件发布）。"""
        self._event_bus = event_bus

    def switch_role(self, role: str) -> bool:
        """切换角色；发布 session:switched 供前端/调度官感知。"""
        if role not in VALID_ROLES:
            return False
        self.role = role
        if self._event_bus is not None:
            self._event_bus.publish(SESSION_SWITCHED, role=role,
                                    session_id=self.session_id)
        return True

    def switch_scene(self, scene: str) -> bool:
        if scene not in VALID_SCENES:
            return False
        self.scene = scene
        return True

    def snapshot(self) -> Dict[str, Any]:
        return {"session_id": self.session_id, "role": self.role,
                "scene": self.scene, "live_mode": self.live_mode,
                "started_at": self.started_at}
