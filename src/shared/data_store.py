"""data_store.py — 键值持久化存储（规格书 6.4）

- 键值持久化（JSON 文件），供记忆调度官、会话恢复、成本统计使用。
- 接口：get(key) / set(key, value) / delete(key) / snapshot()
- 线程安全（RLock）；写入即落盘（autosave=True 时）。

# 模块内容清单（8 项契约）
1. 模块身份标识：shared · DataStore · 对外接口 get()/set()/delete()/snapshot()
2. 配置契约：无外部配置（path/autosave 构造注入，默认 data/data_store.json）
3. 输入契约：get(key, default) 读取；set(key, value) 写入；delete(key) 删除
4. 输出契约：get() 返回值或 default；delete() 返回是否删除；snapshot() 返回完整副本；autosave 时即写即落盘
5. 依赖声明：json、threading、pathlib、src.shared.config_loader
6. 错误定义：加载损坏 JSON 捕获 JSONDecodeError/OSError 回退空字典；_save() 无捕获，IO 异常向上抛
7. 生命周期方法：__init__ 时 _load()；set()/delete() 触发 _save()
8. 领域状态说明：_data 内存键值字典、_mutex RLock 线程锁、_path 落盘路径、_autosave 开关
"""
import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from src.shared.config_loader import PROJECT_ROOT

DEFAULT_DATA_FILE = PROJECT_ROOT / "data" / "data_store.json"


class DataStore:
    def __init__(self, path: Optional[str] = None, autosave: bool = True):
        self._path = Path(path) if path else DEFAULT_DATA_FILE
        self._autosave = autosave
        self._mutex = threading.RLock()
        self._data: Dict[str, Any] = {}
        self._load()

    def get(self, key: str, default: Any = None) -> Any:
        with self._mutex:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._mutex:
            self._data[key] = value
            if self._autosave:
                self._save()

    def delete(self, key: str) -> bool:
        with self._mutex:
            if key not in self._data:
                return False
            del self._data[key]
            if self._autosave:
                self._save()
            return True

    def snapshot(self) -> Dict[str, Any]:
        with self._mutex:
            return dict(self._data)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (json.JSONDecodeError, OSError):
            self._data = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
