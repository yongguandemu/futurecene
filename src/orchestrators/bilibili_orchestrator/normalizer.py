"""normalizer.py — B站事件 → 归一化事件（规格书 5.4 / 582 行）

B站直播开放平台原始消息归一化为 AudienceEvent，发布统一事件：
danmaku:received / gift:received / guard:received / superchat:received / audience:entered。

B站调度官不做弹幕内容处理（规格书 635 行），只归一化发布，消费由指挥官编排。

# 模块内容清单（8 项契约）
1. 模块身份标识：bilibili 调度官 · normalizer · 能力 B站原始消息归一化
2. 配置契约：无（纯函数，无配置读取）
3. 输入契约：normalize(raw: Dict) — B站原始 WS 消息；publish(event_bus, event: AudienceEvent)
4. 输出契约：normalize 返回 AudienceEvent|None；publish 发布 danmaku:received/gift:received/guard:received/superchat:received/audience:entered
5. 依赖声明：time/dataclasses/typing；src.shared.events
6. 错误定义：无法识别的 cmd → normalize 返回 None（不抛异常）
7. 生命周期方法：无（模块级函数 normalize/publish）
8. 领域状态说明：无（无状态；CMD_TO_TYPE/TYPE_TO_EVENT 为常量映射）
"""
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from src.shared.events import (
    AUDIENCE_ENTERED,
    DANMAKU_RECEIVED,
    GIFT_RECEIVED,
    GUARD_RECEIVED,
    SUPERCHAT_RECEIVED,
)

# cmd → 归一化类型（沿用旧项目 bilibili_adapter._map_message 的判定）
CMD_TO_TYPE = {
    "DANMU_MSG": "danmaku",
    "SEND_GIFT": "gift",
    "GUARD_BUY": "guard",
    "SUPER_CHAT_MESSAGE": "super_chat",
    "INTERACT_WORD": "interact",
}

TYPE_TO_EVENT = {
    "danmaku": DANMAKU_RECEIVED,
    "gift": GIFT_RECEIVED,
    "guard": GUARD_RECEIVED,
    "super_chat": SUPERCHAT_RECEIVED,
    "interact": AUDIENCE_ENTERED,
}


@dataclass
class AudienceEvent:
    """归一化观众事件（多平台统一结构）。"""
    event_type: str  # danmaku / gift / guard / super_chat / interact
    content: str  # 弹幕文本 / 礼物描述等
    user_name: str
    user_id: str
    extra: Dict[str, Any] = field(default_factory=dict)  # 平台特有字段（礼物数量等）
    timestamp: float = field(default_factory=time.time)


def normalize(raw: Dict[str, Any]) -> Optional[AudienceEvent]:
    """B站原始消息 → AudienceEvent；无法识别返回 None。"""
    cmd = raw.get("cmd", "")
    data = raw.get("data", {}) or {}
    event_type = CMD_TO_TYPE.get(cmd)
    if event_type is None:
        for prefix, etype in CMD_TO_TYPE.items():
            if cmd.startswith(prefix):
                event_type = etype
                break
    if event_type is None:
        return None

    user_name = data.get("user_name") or data.get("nickname") or data.get("uname") or ""
    user_id = str(data.get("user_id") or data.get("uid") or "")

    if event_type == "danmaku":
        content = data.get("text") or ""
    elif event_type == "gift":
        gift_name = data.get("gift_name") or data.get("giftName") or "礼物"
        num = data.get("num", 1)
        content = f"{gift_name} x{num}"
    elif event_type == "guard":
        content = f"上舰 Lv{data.get('guard_level', 1)}"
    elif event_type == "super_chat":
        content = data.get("message") or ""
    else:  # interact（进场/关注/分享）
        content = data.get("action") or "进场"

    return AudienceEvent(
        event_type=event_type,
        content=content,
        user_name=user_name,
        user_id=user_id,
        extra=data,
    )


def publish(event_bus, event: AudienceEvent) -> None:
    """发布归一化事件（事件载荷只传轻量字段，原始数据不入事件，规格书 3.5）。"""
    event_name = TYPE_TO_EVENT.get(event.event_type)
    if event_name is None:
        return
    event_bus.publish(
        event_name,
        event_type=event.event_type,
        content=event.content,
        user_name=event.user_name,
        user_id=event.user_id,
        extra=event.extra,
        timestamp=event.timestamp,
    )
