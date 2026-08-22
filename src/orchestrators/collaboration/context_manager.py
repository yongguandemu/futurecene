"""context_manager.py — 多角色上下文：每角色独立记忆分桶 + 全局对话流 + 感知彼此注入。

# 模块内容清单 — context_manager

## 1. 模块身份标识
- 所属调度官：collaboration（多角色协作域）
- 能力名：collab:context（多角色上下文组装，间接）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| session_id | 否 | "default" | str | 会话标识（记忆分桶前缀） |
| max_transcript | 否 | 50 | int，>=1 | 全局对话流上限 |
| max_partner_lines | 否 | 2 | int，>=1 | 「感知彼此」注入的对方最近发言条数 |

## 3. 输入契约
- 输入格式：`memory_key(role)` / `record_turn(role, text)` / `global_transcript(limit)` / `partner_lines(speaker, limit)` / `build_system_prompt(role, base_prompt, awareness_enabled, partner_lines)`
- role/speaker：str，角色名；text：str，发言文本

## 4. 输出契约
- 成功：`memory_key()` 返回 str（记忆分桶键）；`global_transcript()/partner_lines()` 返回 str 列表；`build_system_prompt()` 返回 str（组合后的系统提示）
- 失败：无异常路径（文本为空时静默跳过）
- 事件：无

## 5. 依赖声明
- 外部服务：无
- 内部模块：typing（纯标准库）
- 预先配置：无

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 无（纯内存结构） | - | 空文本直接忽略 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start/stop | 否 | 无（随 coordinator 生命周期） |

## 8. 领域状态说明
- 状态项：`_transcript`（全局对话流环形缓冲，上限 max_transcript）
- 持久化：无（记忆持久化由 memory 调度官按 memory_key 分桶负责）
- 恢复：无（对话流随进程生命周期）
"""
from typing import List, Optional


class ContextManager:
    def __init__(self, session_id: str = "default", max_transcript: int = 50,
                 max_partner_lines: int = 2):
        self._session_id = session_id
        self._max_transcript = max_transcript
        self._max_partner_lines = max_partner_lines
        self._transcript: List[str] = []

    def memory_key(self, role: str) -> str:
        """记忆分桶键（配合 memory:retrieve/store 的 character_id）。"""
        return f"{self._session_id}:{role}"

    def record_turn(self, role: str, text: str) -> None:
        if text:
            self._transcript.append(f"{role}: {text}")
            if len(self._transcript) > self._max_transcript:
                self._transcript = self._transcript[-self._max_transcript:]

    def global_transcript(self, limit: int = 0) -> List[str]:
        n = limit if limit > 0 else self._max_transcript
        return list(self._transcript[-n:])

    def partner_lines(self, speaker: str, limit: int = 0) -> List[str]:
        """对方最近发言（感知彼此数据源）。"""
        n = limit if limit > 0 else self._max_partner_lines
        partner = [ln for ln in self._transcript
                   if not ln.startswith(speaker + ":")]
        return partner[-n:]

    def build_system_prompt(self, role: str, base_prompt: str = "",
                            awareness_enabled: bool = True,
                            partner_lines: Optional[List[str]] = None) -> str:
        prompt = base_prompt
        if awareness_enabled:
            lines = partner_lines if partner_lines is not None else self.partner_lines(role)
            if lines:
                prompt += ("\n\n【感知彼此】对方最近发言如下（与你同台的主播）。回应观众时"
                           "自然地与对方发言衔接（接话/回应/吐槽均可），不要无视对方"
                           "刚说过的话；若对方发言与本轮弹幕无关，正常回应观众即可。\n"
                           + "\n".join(lines))
        return prompt.strip()
