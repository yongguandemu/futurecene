"""intent_parser.py — 意图解析（规格书 4.4，输入边界）

只处理需要角色回应的输入（弹幕/私信/语音/前端命令）；
平台事件（礼物/上舰等）不走 Intent Parser，直接发布归一化事件。
输出统一结构化命令对象 Command。

# 模块内容清单（8 项契约）
1. 模块身份标识：commander · IntentParser · 对外 parse()；输出 Command 数据类
2. 配置契约：无独立配置；内置规则正则（切换角色/点歌/状态/系统命令）
3. 输入契约：parse(text, source, session_id) 原始文本
4. 输出契约：Command（capability/payload/source/session_id/raw/command_id）
5. 依赖声明：re、dataclasses、typing
6. 错误定义：空文本与未知意图默认 llm:chat；! 前缀未匹配归 system:command
7. 生命周期方法：parse()（无状态）
8. 领域状态说明：无状态；仅模块级正则常量（_SWITCH_ROLE_RE 等）
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict

_SWITCH_ROLE_RE = re.compile(r"^!切换\s*(yuki|lilith)$", re.IGNORECASE)
# 自然语言角色切换（验收契约）：切换到 Lilith / 切换角色为 Lilith / 切换成 Yuki
_SWITCH_ROLE_NL_RE = re.compile(r"^切换(?:角色)?\s*(?:为|到|成)?\s*(yuki|lilith)$", re.IGNORECASE)
_POINT_SONG_RE = re.compile(r"^!点歌\s+(.+)$")
_STATUS_RE = re.compile(r"^!(状态|status)$", re.IGNORECASE)
# 直播准备意图（直播间集成 · 智能助手联动）
_LIVE2D_LOAD_RE = re.compile(r"^(?:加载|切换)?(?:模型)?\s*(hiyori|haru|小恶魔|yuki|lilith)\s*(?:模型)?$",
                             re.IGNORECASE)
_LIVE2D_EXPRESSION_RE = re.compile(r"^做(?:个)?(开心|难过|惊讶|害羞|生气|平静)(?:的表情)?$")
_LIVE2D_MOTION_RE = re.compile(r"^(挥挥手|挥手|点头|摇头|打招呼)$")
_LIVE2D_PREPARE_RE = re.compile(r"^准备(?:一下)?(?:直播|开播)(?:界面)?$")
# OBS 浏览器源（直播间源登记 · 智能助手联动）：查询清单 / 打开指定源
_OBS_SOURCES_RE = re.compile(r"^(?:有哪些)?(?:OBS|obs|浏览器|直播|开播)源(?:地址|清单|有哪些|是什么)?$")
_OBS_OPEN_RE = re.compile(r"^(?:打开|启动)(?:OBS|obs|浏览器)?(?:的)?(.{1,8})源$")


@dataclass
class Command:
    """结构化命令（规格书 4.4）。"""
    capability: str  # 能力名，如 "llm:chat"
    payload: Dict[str, Any]  # 结构化参数
    source: str  # danmaku / command / voice / system
    session_id: str  # 会话 ID
    raw: str = ""  # 原始文本（审计/调试）
    command_id: str = ""  # 命令追踪 ID（路由层生成，事件透传）


class IntentParser:
    """规则驱动意图识别（v1.0 最小规则集，后续可升级模型驱动）。"""

    def parse(self, text: str, source: str = "danmaku",
              session_id: str = "default") -> Command:
        """文本 → Command。"""
        raw = (text or "").strip()
        if not raw:
            return Command(capability="llm:chat", payload={"text": ""},
                           source=source, session_id=session_id, raw=raw)

        # 规则集（规格书 4.4 意图表）
        m = _SWITCH_ROLE_RE.match(raw)
        if m:
            return Command(capability="session:switch",
                           payload={"role": m.group(1).lower()},
                           source=source, session_id=session_id, raw=raw)

        m = _SWITCH_ROLE_NL_RE.match(raw)
        if m:
            return Command(capability="session:switch",
                           payload={"role": m.group(1).lower()},
                           source=source, session_id=session_id, raw=raw)

        m = _POINT_SONG_RE.match(raw)
        if m:
            # 点歌：P2 后接入 music:request；当前归入 llm:chat（人设驱动回应）
            # TODO: 确认 — music:request 能力 P2 后注册，届时改路由
            return Command(capability="llm:chat",
                           payload={"text": raw, "intent": "request_song"},
                           source=source, session_id=session_id, raw=raw)

        m = _STATUS_RE.match(raw)
        if m:
            return Command(capability="system:status", payload={},
                           source=source, session_id=session_id, raw=raw)

        # 直播准备意图（直播间集成 · 智能助手联动）
        m = _LIVE2D_LOAD_RE.match(raw)
        if m:
            name = m.group(1).lower()
            return Command(capability="live2d:load",
                           payload={"model_name": "小恶魔" if name in ("小恶魔", "lilith")
                                    else "Hiyori"},
                           source=source, session_id=session_id, raw=raw)

        m = _LIVE2D_EXPRESSION_RE.match(raw)
        if m:
            return Command(capability="live2d:expression",
                           payload={"expression": m.group(1)},
                           source=source, session_id=session_id, raw=raw)

        m = _LIVE2D_MOTION_RE.match(raw)
        if m:
            mapping = {"挥挥手": "wave", "挥手": "wave", "点头": "nod",
                       "摇头": "shake", "打招呼": "wave"}
            return Command(capability="live2d:motion",
                           payload={"motion": mapping[m.group(1)]},
                           source=source, session_id=session_id, raw=raw)

        m = _LIVE2D_PREPARE_RE.match(raw)
        if m:
            return Command(capability="live2d:prepare", payload={},
                           source=source, session_id=session_id, raw=raw)

        # OBS 浏览器源：查询清单 / 打开指定源（key 由 obs_sources.resolve_key 解析别名）
        m = _OBS_SOURCES_RE.match(raw)
        if m:
            return Command(capability="obs:sources", payload={},
                           source=source, session_id=session_id, raw=raw)

        m = _OBS_OPEN_RE.match(raw)
        if m:
            return Command(capability="obs:open",
                           payload={"key": m.group(1).strip().lower()},
                           source=source, session_id=session_id, raw=raw)

        # 系统命令（! 前缀未匹配到已知规则）→ 指挥官内部处理
        if raw.startswith("!"):
            return Command(capability="system:command",
                           payload={"text": raw},
                           source=source, session_id=session_id, raw=raw)

        # 默认：闲聊对话 → llm:chat（规格书 4.4：未知意图默认路由 llm:chat）
        return Command(capability="llm:chat", payload={"text": raw},
                       source=source, session_id=session_id, raw=raw)
