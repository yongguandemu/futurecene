"""user_config.py — 用户设置存储（任务五：运营可读设置持久化）

设置面板的 4 项设置（memory_strength / tts_output_target / allow_memory_to_worldbook /
reasoning_intensity）持久化到 data/config_user.json（不写 config.yaml 模板）。

# 模块内容清单 — user_config

## 1. 模块身份标识
- 所属调度官：shared（无调度官归属，装配层组件）
- 能力名：无（提供 get/set/all，供 /api/config 与装配层消费）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| memory_strength | 否 | medium | low/medium/high/ultra | 记忆强度 → recall k（2/5/10/15） |
| tts_output_target | 否 | local | local/stream/both | 语音输出目标（替代单布尔开关） |
| allow_memory_to_worldbook | 否 | false | bool | L3→世界书提案（off=不生成） |
| reasoning_intensity | 否 | standard | power_save/standard/enhanced | 推理强度 → engine/长度/温度 |

## 3. 输入契约
- 输入格式：`UserConfigStore(data_file=None)`（缺省 data/config_user.json）
- 输入格式：`get(key)` / `set(key, value)` / `all()`

## 4. 输出契约
- 成功：get 返回当前值（默认或用户覆盖）；set 校验通过并持久化返回 True
- 失败：非法值抛 ValueError（提示合法取值）；set 返回 False 仅用于文件写入失败
- 事件：无

## 5. 依赖声明
- 外部服务：无
- 内部模块：src.shared.config_loader（PROJECT_ROOT）
- 预先配置：无（data/ 缺省自动创建）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| ValueError | set 传非法值 | 路由层捕获返回 400 |
| OSError | 文件不可写 | set 返回 False，调用方记录日志 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| get(key) | 是 | 默认值 + 用户覆盖合并取值 |
| set(key, value) | 是 | 校验 + 持久化 |
| all() | 是 | 返回全部设置当前值 |

## 8. 领域状态说明
- 状态项：_values（用户覆盖 dict，内存缓存）
- 持久化：data/config_user.json（读写全量快照）
- 恢复：构造时读取磁盘恢复
"""
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from src.shared.config_loader import PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_USER_FILE = PROJECT_ROOT / "data" / "config_user.json"

# 默认值 + 合法取值（设置面板 4 项，规格书 5.2）
DEFAULTS: Dict[str, Any] = {
    "memory_strength": "medium",
    "tts_output_target": "local",
    "allow_memory_to_worldbook": False,
    "reasoning_intensity": "standard",
}
VALID_VALUES: Dict[str, set] = {
    "memory_strength": {"low", "medium", "high", "ultra"},
    "tts_output_target": {"local", "stream", "both"},
    "reasoning_intensity": {"power_save", "standard", "enhanced"},
}


class UserConfigStore:
    """用户设置存储（默认值 + 用户覆盖合并）。"""

    def __init__(self, data_file: Optional[str] = None):
        self._file = Path(data_file) if data_file else DEFAULT_CONFIG_USER_FILE
        self._lock = threading.RLock()
        self._values: Dict[str, Any] = {}
        self._load()

    def get(self, key: str) -> Any:
        """当前值：用户覆盖优先，缺省回落默认。"""
        with self._lock:
            if key in self._values:
                return self._values[key]
            return DEFAULTS.get(key)

    def set(self, key: str, value: Any) -> bool:
        """校验并持久化；非法值抛 ValueError。"""
        if key not in DEFAULTS:
            raise ValueError(f"未知设置项: {key}")
        allowed = VALID_VALUES.get(key)
        if allowed is not None:
            value = str(value).lower() if isinstance(value, str) else value
            if value not in allowed:
                raise ValueError(f"设置 {key} 取值非法: {value!r}（可选 {sorted(allowed)}）")
        else:
            value = bool(value)
        with self._lock:
            self._values[key] = value
        ok = self._save()
        logger.info("[UserConfig] %s=%s（%s）", key, value, "已持久化" if ok else "持久化失败")
        return ok

    def all(self) -> Dict[str, Any]:
        """全部设置当前值（默认 + 覆盖合并）。"""
        return {k: self.get(k) for k in DEFAULTS}

    def _load(self) -> None:
        try:
            if not self._file.exists():
                return
            data = json.loads(self._file.read_text(encoding="utf-8"))
            self._values = {k: v for k, v in data.items() if k in DEFAULTS}
        except (OSError, ValueError) as e:  # pragma: no cover - 防御
            logger.warning("[UserConfig] 读取失败（忽略，重建）: %s", e)
            self._values = {}

    def _save(self) -> bool:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(json.dumps(self._values, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
            return True
        except OSError as e:  # pragma: no cover - 防御
            logger.error("[UserConfig] 持久化失败: %s", e)
            return False
