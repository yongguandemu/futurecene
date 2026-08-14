"""vn_session.py — VN 陪看核心（P3，合并旧项目三套实现：visual_novel/ + vn_adapter.py + vn_bridge.py）

设计（规格书 11.4 合并说明）：
- VNProfile：作品配置（atri.yaml 风格：名称/窗口标题/点击推进坐标/状态关键词）。
- VNSession：会话状态机（对白/选项/菜单），内部定时轮询（capture + ocr）判断画面状态，
  自动点击推进；状态变化发布 game:vn_state_changed。
- 职责边界（5.5）：解说生成不直接调 LLM，发布 game:commentary_requested。

# 模块内容清单（8 项契约）
1. 模块身份标识：game 调度官 · vn_session · 能力 game:vn_start/vn_stop/vn_state（承载实现）
2. 配置契约：VNProfile（config/visual_novel_profiles/{name}.json，默认 atri）；poll_interval=2.0s
3. 输入契约：VNSession(profile, screen_orchestrator, event_bus)；start()/stop()/snapshot()；load_profile(name)
4. 输出契约：snapshot 返回 Dict；状态变化发布 game:vn_state_changed；解说请求发布 game:commentary_requested
5. 依赖声明：json/logging/threading/time/dataclasses/pathlib/typing；src.shared.config_loader、src.shared.events
6. 错误定义：轮询/自动推进异常 → 捕获并 logger.error，不影响主循环；配置文件缺失 → 内置默认 profile
7. 生命周期方法：start()/stop()；_poll_loop 线程随 start 启动、stop 结束
8. 领域状态说明：state（对白/选项/菜单/unknown）、_thread、_running、_lock
"""
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.shared.config_loader import PROJECT_ROOT
from src.shared.events import GAME_COMMENTARY_REQUESTED, GAME_VN_STATE_CHANGED

logger = logging.getLogger(__name__)

VN_PROFILES_DIR = PROJECT_ROOT / "config" / "visual_novel_profiles"
DEFAULT_PROFILE = "atri"


@dataclass
class VNProfile:
    """VN 作品配置（对应旧项目 atri.yaml）。"""
    name: str
    window_title: str  # 游戏窗口标题（供 PrintWindow 捕获）
    click_coords: tuple = (960, 940)  # 推进对话的点击坐标
    start_btn_coords: tuple = (960, 800)  # 开始按钮坐标
    advance_keys: List[str] = field(default_factory=lambda: ["ENTER", "SPACE"])
    state_keywords: Dict[str, List[str]] = field(default_factory=lambda: {
        "对白": ["next", "continue", "开始"],
        "菜单": ["new game", "continue", "load", "设置"],
        "选项": ["选择", "option"],
    })

    @classmethod
    def from_json(cls, path: Path) -> "VNProfile":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(name=data.get("name", path.stem),
                   window_title=data.get("window_title", ""),
                   click_coords=tuple(data.get("click_coords", (960, 940))),
                   start_btn_coords=tuple(data.get("start_btn_coords", (960, 800))),
                   advance_keys=data.get("advance_keys", ["ENTER", "SPACE"]),
                   state_keywords=data.get("state_keywords", {}))


class VNSession:
    """VN 陪看会话：状态机 + 内部定时推进。"""

    def __init__(self, profile: VNProfile, screen_orchestrator=None,
                 event_bus=None, poll_interval: float = 2.0):
        self.profile = profile
        self._screen = screen_orchestrator  # 屏幕控制调度官（命令调用）
        self._event_bus = event_bus
        self._poll_interval = poll_interval
        self.state: str = "unknown"
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.RLock()

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True,
                                        name=f"VN-{self.profile.name}")
        self._thread.start()
        logger.info("[VNSession] 已启动: %s", self.profile.name)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    # ---------- 状态 ----------

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"profile": self.profile.name, "state": self.state,
                    "running": self._running}

    # ---------- 内部：轮询推进 ----------

    def _poll_loop(self) -> None:
        while self._running:
            try:
                new_state = self._detect_state()
                with self._lock:
                    changed = new_state != self.state
                    self.state = new_state
                if changed and self._event_bus:
                    self._event_bus.publish(GAME_VN_STATE_CHANGED, state=new_state,
                                            profile=self.profile.name)
                    logger.info("[VNSession] 状态: %s", new_state)
                self._auto_advance(new_state)
            except Exception as e:
                logger.error("[VNSession] 轮询异常: %s", e)
            time.sleep(self._poll_interval)

    def _detect_state(self) -> str:
        """基于 OCR 文本判断画面状态（对白/菜单/选项/unknown）。

        # TODO: 确认 — 需真实游戏窗口联调；无屏幕时返回 unknown。
        """
        if self._screen is None:
            return self.state or "unknown"
        try:
            result = self._screen.handle({"capability": "screen:capture",
                                          "payload": {"window_title": self.profile.window_title,
                                                      "with_ocr": True}})
            text = (result.get("data", {}).get("text") or "").lower()
            if not text:
                return "unknown"
            for state, keywords in self.profile.state_keywords.items():
                if any(k in text for k in keywords):
                    return state
            return "对白"
        except Exception:
            return self.state or "unknown"

    def _auto_advance(self, state: str) -> None:
        """自动推进：菜单→点击开始；对白→点击推进；选项→点击第一个选项。"""
        if self._screen is None:
            return
        try:
            if state == "菜单":
                x, y = self.profile.start_btn_coords
                self._screen.handle({"capability": "screen:click",
                                     "payload": {"x": x, "y": y}})
            elif state == "对白":
                x, y = self.profile.click_coords
                self._screen.handle({"capability": "screen:click",
                                     "payload": {"x": x, "y": y}})
            elif state == "选项":
                # 默认点击第一个选项（配置可覆盖）
                self._screen.handle({"capability": "screen:keypress",
                                     "payload": {"key": "DOWN"}})
                self._screen.handle({"capability": "screen:keypress",
                                     "payload": {"key": "ENTER"}})
        except Exception as e:
            logger.error("[VNSession] 自动推进失败: %s", e)


def load_profile(name: str = DEFAULT_PROFILE) -> VNProfile:
    """加载作品配置（config/visual_novel_profiles/{name}.json）；缺失时用内置默认。"""
    path = VN_PROFILES_DIR / f"{name}.json"
    if path.exists():
        return VNProfile.from_json(path)
    # 内置默认配置（atri 风格），避免无配置文件时不可用
    logger.warning("[VNSession] 配置文件缺失，使用内置默认: %s", path)
    return VNProfile(name=name, window_title=name)
