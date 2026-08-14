"""模块内容清单 — commentary_policy

## 1. 模块身份标识
- 所属调度官：live_intelligence_orchestrator
- 能力名：intel:commentary_generate / commentary_set_style
- 引擎名：（单实现）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| style | 否 | "陪看吐槽型" | str | 解说风格 |
| max_words | 否 | 40 | int, 8-200 | 解说最大字数 |

## 3. 输入契约
- intel:commentary_generate 输入：{"state": str, "text"?: str}
  - state 必填，str ∈ {dialogue,choice,menu,puzzle,transition,cg,unknown}
  - text 可选，str，当前画面文本
- commentary_set_style 输入：{"style": str}

## 4. 输出契约
- 成功：{"ok": true, "data": {"commentary": str, "state": str}, "error": null}
- 失败：{"ok": false, "data": {}, "error": str}

## 5. 依赖声明
- 外部服务：无
- 内部模块：vn_screen_state（VNScreenState）、shared/events（COMMENTARY_GENERATED，可选）
- 预先配置：无

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| ValueError | state 非法 | 调用方校验输入 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| init | 是 | 读取风格与字数配置 |
| start/stop | 否 | 纯规则引擎，无生命周期 |
| health | 是 | 返回风格与字数配置 |

## 8. 领域状态说明
- 状态项：_style / _max_words
- 持久化：无
- 恢复：无
"""
import logging
from typing import Any, Dict, Optional

from src.orchestrators.live_intelligence_orchestrator.vn_screen_state import VNScreenState
from src.shared.events import COMMENTARY_GENERATED

logger = logging.getLogger(__name__)

_VALID_STATES = {"dialogue", "choice", "menu", "puzzle", "transition", "cg", "unknown"}


class VNCommentaryPolicy:
    """VN 陪看解说策略 — 生成短句吐槽，避免抢剧情。"""

    def __init__(self, event_bus=None, style: str = "陪看吐槽型", max_words: int = 40):
        self.event_bus = event_bus
        self.style = style
        self.max_words = int(max_words)

    def health(self) -> Dict[str, Any]:
        return {"status": "ok", "detail": f"style={self.style}, max_words={self.max_words}"}

    # ---------- 核心操作 ----------

    def generate(self, state: VNScreenState) -> str:
        """根据画面状态生成解说短句。"""
        s = state.state
        if s == "choice":
            text = "这里出现选项了，我先停一下，我们可以看看哪个选择更像主角会做的事。"
        elif s == "puzzle":
            text = "这里像是解谜段落，我先不乱点，等看清线索再动手。"
        elif s == "menu":
            text = "现在像是在菜单界面，我先确认一下状态，避免误操作。"
        elif s == "transition":
            text = "转场来了，感觉剧情要切到新的信息点了。"
        elif s == "cg":
            text = "这个画面可以稍微停一下，氛围感已经拉起来了。"
        elif s == "dialogue":
            text = "这段对白有点值得品一下，角色的情绪已经在往外露了。"
        else:
            text = "我先观察一下画面状态，不确定的时候不乱操作。"
        return self._trim(text)

    def generate_commentary(self, state: str, text: str = "") -> str:
        """字符串接口，供调度官 handle 调用。"""
        state = (state or "").strip().lower()
        if state not in _VALID_STATES:
            raise ValueError(f"state must be one of {sorted(_VALID_STATES)}")
        return self.generate(VNScreenState(state=state, text=text))

    def publish_generated(self, state: str, commentary: str) -> None:
        if self.event_bus:
            try:
                self.event_bus.publish(COMMENTARY_GENERATED, state=state,
                                       commentary=commentary)
            except Exception as e:
                logger.warning("[CommentaryPolicy] 发布事件失败: %s", e)

    # ---------- 内部 ----------

    def _trim(self, text: str) -> str:
        limit = max(8, self.max_words * 2)
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"