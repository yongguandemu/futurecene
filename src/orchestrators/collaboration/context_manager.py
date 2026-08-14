"""context_manager.py — 多角色上下文：每角色独立记忆分桶 + 全局对话流 + 感知彼此注入。"""
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
                prompt += "\n\n【感知彼此】对方最近发言：\n" + "\n".join(lines)
        return prompt.strip()
