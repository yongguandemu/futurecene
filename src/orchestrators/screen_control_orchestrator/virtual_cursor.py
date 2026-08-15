"""virtual_cursor.py — 双角色虚拟光标（规格书 5.4 screen:cursor 扩展）

原系统（LumiProject/core/virtual_cursor.py）仅重导出不存在的
virtual_cursor_manager 模块，虚拟光标从未真正可用。本模块补齐：
- 双角色（yuki/lilith）光标状态机：位置/可见性/活跃/轨迹/点击波纹/操作标签
- 经 EventBus 订阅 screen:cursor_action / screen:cursor_active_role /
  screen:cursor_visibility，与 ScreenControlOrchestrator 输入层联动
- 渲染双通道：Win32 透明覆盖窗口（GDI，无 PyQt 依赖）+ 前端事件
  （screen:cursor_state 推送，供 dashboard 可视化）
- 角色切换时旧光标淡出待机、新光标高亮（沿用原系统设计）

# 模块内容清单 — virtual_cursor

## 1. 模块身份标识
- 所属调度官：screen
- 能力名：screen:cursor（虚拟光标状态管理 + 渲染）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| overlay_enabled | 否 | True | bool | 是否启用 Win32 覆盖窗口渲染（无窗口环境自动降级） |
| render_fps | 否 | 30 | int | 覆盖窗口重绘帧率 |

## 3. 输入契约
- 输入格式：`VirtualCursorManager(event_bus, overlay=None)`；`start()/stop()/get_status()/get_cursor_state(role)`
- 事件订阅：screen:cursor_action（action/x/y/label/role）、screen:cursor_active_role（active_role/previous_role）、screen:cursor_visibility（role/visible/fade_duration）

## 4. 输出契约
- 成功：start() 返回 True；get_status() 返回 {active_role, overlay_available, cursors}
- 失败：overlay 不可用时渲染降级为仅事件推送（逻辑不受影响）
- 事件：发布 screen:cursor_state（role/x/y/visible/active/label/trail/ripples，供前端）

## 5. 依赖声明
- 外部服务：无（Win32 GDI 经 ctypes，可选）
- 内部模块：src.shared.events、cursor_overlay（可选）
- 预先配置：ScreenControlOrchestrator 构造时创建并 start()

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 覆盖窗口创建失败 | 无桌面/权限受限 | overlay_available=False，降级为事件推送 |
| 事件发布失败 | EventBus 异常 | 捕获记录，不阻断光标状态更新 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start | 是 | 初始化覆盖窗口 + 订阅事件 + 启动渲染线程 |
| stop | 是 | 隐藏覆盖窗口 + 停止渲染线程 |

## 8. 领域状态说明
- 状态项：_cursors（role→RoleCursor）、_active_role、_overlay
- 持久化：无
- 恢复：无状态持久化；start() 重建
"""
import collections
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from src.shared.events import (
    SCREEN_CURSOR_ACTION,
    SCREEN_CURSOR_ACTIVE_ROLE,
    SCREEN_CURSOR_STATE,
    SCREEN_CURSOR_VISIBILITY,
)

logger = logging.getLogger(__name__)

# 角色光标配色（沿用原系统设计：yuki 蓝 / lilith 红）
CURSOR_THEMES = {
    "yuki": {
        "color": "#60a5fa",
        "shape": "diamond_arrow",
        "ripple_color": (96, 165, 250, 100),
        "label_bg": (96, 165, 250, 180),
        "trail_color": (96, 165, 250, 80),
    },
    "lilith": {
        "color": "#ef4444",
        "shape": "sharp_arrow",
        "ripple_color": (239, 68, 68, 100),
        "label_bg": (239, 68, 68, 180),
        "trail_color": (239, 68, 68, 80),
    },
}


class RippleAnimation:
    """点击波纹动画（纯数据，渲染层消费）。"""

    def __init__(self, x: int, y: int, color, duration: float = 0.5):
        self.x = x
        self.y = y
        self.color = color
        self.duration = duration
        self.start_time = time.time()

    def alive(self) -> bool:
        return time.time() - self.start_time < self.duration

    def progress(self) -> float:
        return min(1.0, (time.time() - self.start_time) / self.duration)


class RoleCursor:
    """单个角色的虚拟光标实例。"""

    def __init__(self, role: str, theme: Dict[str, Any], overlay=None):
        self.role = role
        self.theme = theme
        self._overlay = overlay
        self._x = 0
        self._y = 0
        self._visible = True
        self._active = False
        self._trail: "collections.deque" = collections.deque(maxlen=30)
        self._ripples: List[RippleAnimation] = []
        self._label_text = ""

    def handle_action(self, action: str, x: Optional[int] = None,
                      y: Optional[int] = None, label: str = "") -> None:
        """处理光标动作（move/click/double_click/right_click）。"""
        if x is not None:
            self._x = int(x)
        if y is not None:
            self._y = int(y)
        self._label_text = label or self._label_text

        if action in ("click", "double_click", "right_click"):
            self._ripples.append(RippleAnimation(
                self._x, self._y, self.theme.get("ripple_color", (255, 255, 255, 100))))
        elif action == "move":
            self._trail.append((self._x, self._y, time.time()))

        self._ripples = [r for r in self._ripples if r.alive()]

        if self._overlay is not None:
            try:
                self._overlay.request_render(self)
            except Exception as e:
                logger.debug("[VirtualCursor] 渲染请求失败: %s", e)

    def set_active(self, active: bool) -> None:
        self._active = active
        if not active:
            self._label_text = ""

    def set_visible(self, visible: bool, fade_duration: float = 0.3) -> None:
        self._visible = visible
        if self._overlay is not None:
            try:
                self._overlay.request_fade(self.role, visible, fade_duration)
            except Exception as e:
                logger.debug("[VirtualCursor] 淡入淡出请求失败: %s", e)

    @property
    def opacity(self) -> float:
        """当前透明度：活跃 1.0 / 待机 0.35 / 隐藏 0.0。"""
        if not self._visible:
            return 0.0
        return 1.0 if self._active else 0.35

    @property
    def position(self):
        return (self._x, self._y)

    @property
    def label(self) -> str:
        return self._label_text

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def is_visible(self) -> bool:
        return self._visible

    def get_state(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "x": self._x,
            "y": self._y,
            "visible": self._visible,
            "active": self._active,
            "label": self._label_text,
            "trail": [(x, y) for x, y, _ in self._trail],
            "ripples": [{"x": r.x, "y": r.y, "progress": r.progress()}
                        for r in self._ripples],
        }


class VirtualCursorManager:
    """双角色虚拟光标管理器：状态管理 + 渲染调度 + 前端事件推送。"""

    def __init__(self, event_bus=None, overlay=None):
        self._bus = event_bus
        self._overlay = overlay
        self._cursors = {
            "yuki": RoleCursor("yuki", CURSOR_THEMES["yuki"], overlay),
            "lilith": RoleCursor("lilith", CURSOR_THEMES["lilith"], overlay),
        }
        self._active_role = "yuki"
        self._installed = False
        self._render_thread: Optional[threading.Thread] = None
        self._stop_render = threading.Event()

    # ---------- 生命周期 ----------

    def start(self) -> bool:
        """初始化覆盖窗口（如可用）+ 订阅事件 + 启动渲染线程。"""
        if self._overlay is not None:
            try:
                self._overlay.init_window()
            except Exception as e:
                logger.warning("[VirtualCursor] 覆盖窗口初始化失败: %s", e)
        self.install()
        if not self._stop_render.is_set():
            self._stop_render.clear()
        self._render_thread = threading.Thread(target=self._render_loop, daemon=True,
                                               name="virtual-cursor-render")
        self._render_thread.start()
        self._cursors["yuki"].set_active(True)
        logger.info("[VirtualCursorManager] 已启动（overlay=%s）",
                    self._overlay is not None and self._overlay.is_available())
        return True

    def stop(self) -> None:
        self._stop_render.set()
        if self._overlay is not None:
            try:
                self._overlay.hide()
            except Exception as e:
                logger.debug("[VirtualCursor] 隐藏覆盖窗口失败: %s", e)
        if self._render_thread is not None and self._render_thread.is_alive():
            self._render_thread.join(timeout=3)
        self._render_thread = None
        logger.info("[VirtualCursorManager] 已停止")

    def install(self) -> None:
        """订阅 EventBus 事件（幂等）。"""
        if self._installed or self._bus is None:
            return
        self._bus.subscribe(SCREEN_CURSOR_ACTION, self._on_cursor_action,
                            name="VirtualCursorManager")
        self._bus.subscribe(SCREEN_CURSOR_ACTIVE_ROLE, self._on_role_changed,
                            name="VirtualCursorManager")
        self._bus.subscribe(SCREEN_CURSOR_VISIBILITY, self._on_visibility_changed,
                            name="VirtualCursorManager")
        self._installed = True

    # ---------- 事件处理 ----------

    def _on_cursor_action(self, event=None, **data) -> None:
        role = data.get("role", self._active_role)
        action = data.get("action", "")
        cursor = self._cursors.get(role)
        if cursor is None or not cursor.is_visible:
            return
        cursor.handle_action(action, data.get("x"), data.get("y"),
                             data.get("label", ""))
        self._publish_state(role)

    def _on_role_changed(self, event=None, **data) -> None:
        new_role = data.get("active_role", "")
        old_role = data.get("previous_role", "")
        if old_role and old_role in self._cursors:
            self._cursors[old_role].set_active(False)
        if new_role and new_role in self._cursors:
            self._cursors[new_role].set_active(True)
            self._active_role = new_role
        logger.info("[VirtualCursorManager] 活跃角色切换: %s -> %s",
                    old_role, new_role)
        self._publish_state(new_role)

    def _on_visibility_changed(self, event=None, **data) -> None:
        role = data.get("role", "")
        visible = data.get("visible", True)
        fade_duration = data.get("fade_duration", 0.3)
        if role in self._cursors:
            self._cursors[role].set_visible(visible, fade_duration)
            self._publish_state(role)

    # ---------- 渲染 ----------

    def _render_loop(self) -> None:
        """30Hz 渲染循环：有活跃动画时请求覆盖窗口重绘。"""
        while not self._stop_render.wait(1.0 / 30.0):
            if self._overlay is None:
                continue
            try:
                if not self._overlay.is_available():
                    continue
                if any(self._has_animations(c) for c in self._cursors.values()):
                    self._overlay.request_render(self)
            except Exception as e:
                logger.debug("[VirtualCursor] 渲染循环异常: %s", e)

    @staticmethod
    def _has_animations(cursor: RoleCursor) -> bool:
        return bool(cursor._ripples) or bool(cursor._trail)

    def _publish_state(self, role: str) -> None:
        if self._bus is None:
            return
        try:
            state = self._cursors[role].get_state()
            state["active_role"] = self._active_role
            self._bus.publish(SCREEN_CURSOR_STATE, **state)
        except Exception as e:
            logger.debug("[VirtualCursor] 状态事件发布失败: %s", e)

    # ---------- 查询 ----------

    def get_cursor_state(self, role: Optional[str] = None):
        if role:
            cursor = self._cursors.get(role)
            return cursor.get_state() if cursor else None
        return {r: c.get_state() for r, c in self._cursors.items()}

    def get_active_role(self) -> str:
        return self._active_role

    def get_status(self) -> Dict[str, Any]:
        return {
            "active_role": self._active_role,
            "overlay_available": bool(self._overlay and self._overlay.is_available()),
            "cursors": self.get_cursor_state(),
        }
