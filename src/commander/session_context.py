"""session_context.py — 会话状态（规格书 4.6，状态归属规则 ADR-001）

会话状态（角色/场景/直播模式）归指挥官；领域内部状态归各调度官。
多角色协作：present_roles（在场角色集合）+ lead_role（主角色）构成在场模型，
add_role/remove_role/set_lead 维护在场状态，switch_role 仅切换焦点角色（向后兼容）。

# 模块内容清单（8 项契约）
1. 模块身份标识：commander · SessionContext · 对外 switch_role/switch_scene/snapshot/bind_event_bus/add_role/remove_role/set_lead
2. 配置契约：VALID_ROLES/VALID_SCENES 常量约束合法取值
3. 输入契约：switch_role(role)/switch_scene(scene)/add_role(role)/remove_role(role)/set_lead(role)
4. 输出契约：切换/在场变更返回 bool；snapshot 返回会话状态字典；发布 SESSION_SWITCHED/CHARACTER_PRESENCE_CHANGED 事件
5. 依赖声明：time、dataclasses、typing、shared.events
6. 错误定义：非法角色/场景返回 False（不切换）；角色不在场时 set_lead 返回 False
7. 生命周期方法：bind_event_bus()（装配时绑定 EventBus）
8. 领域状态说明：role/scene/live_mode/started_at/lead_role/present_roles（会话状态归指挥官，ADR-001）
"""
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from src.shared.events import CHARACTER_PRESENCE_CHANGED, SESSION_SWITCHED

VALID_ROLES = {"yuki", "lilith"}
VALID_SCENES = {"chat", "game", "vn", "mc"}


@dataclass
class SessionContext:
    """会话上下文（指挥官持有，唯一状态源）。"""
    session_id: str
    role: str = "yuki"  # 焦点角色（兼容现有逻辑）
    scene: str = "chat"  # chat / game / vn / mc
    live_mode: str = "offline"  # offline / live
    started_at: float = field(default_factory=time.time)
    lead_role: str = "yuki"  # 主角色：系统意图归属（不独占发言权）
    present_roles: set = field(default_factory=lambda: {"yuki"})  # 在场角色集合

    def __post_init__(self):
        self._event_bus: Optional[Any] = None

    def bind_event_bus(self, event_bus) -> None:
        """装配时绑定 EventBus（用于状态变更事件发布）。"""
        self._event_bus = event_bus

    def add_role(self, role: str) -> bool:
        """角色进场；发布 character:presence_changed（present=True）。"""
        if role not in VALID_ROLES or role in self.present_roles:
            return False
        self.present_roles.add(role)
        if self._event_bus is not None:
            self._event_bus.publish(CHARACTER_PRESENCE_CHANGED, role=role,
                                    present=True, session_id=self.session_id)
        return True

    def remove_role(self, role: str) -> bool:
        """角色离场；焦点/主角色在场性回退，发布 presence_changed（present=False）。"""
        if role not in self.present_roles:
            return False
        self.present_roles.discard(role)
        if role == self.role:
            self.role = "yuki" if "yuki" in self.present_roles else min(self.present_roles)
        if role == self.lead_role:
            self.lead_role = self.role
        if self._event_bus is not None:
            self._event_bus.publish(CHARACTER_PRESENCE_CHANGED, role=role,
                                    present=False, session_id=self.session_id)
        return True

    def set_lead(self, role: str) -> bool:
        """设置主角色；仅限在场角色。"""
        if role not in self.present_roles:
            return False
        self.lead_role = role
        return True

    def switch_role(self, role: str) -> bool:
        """切换焦点角色；发布 session:switched 供前端/调度官感知。

        兼容现状：单角色模式切换即在场；多角色模式不改变在场集合。
        """
        if role not in VALID_ROLES:
            return False
        self.role = role
        if role not in self.present_roles:
            self.present_roles.add(role)   # 单角色模式：切换即在场
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
                "started_at": self.started_at, "lead_role": self.lead_role,
                "present_roles": sorted(self.present_roles)}
