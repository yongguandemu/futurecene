"""memory_config.py — 分层记忆配置（任务四）

L0/L1/L2/L3 保留期、压缩阈值、压缩模型选择、记忆强度→检索条数映射。
配置来源为 ConfigLoader 的 memory.* 段（config/config.yaml），未配置时使用内置默认值。

# 模块内容清单 — memory_config

## 1. 模块身份标识
- 所属调度官：memory（记忆调度官）
- 能力名：无独立能力，为 memory 域全部子模块提供配置

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| memory.l0_retention_days | 否 | 14 | int 7..30 | L0 原始日志保留天数（2-4 周） |
| memory.l1.window_sec | 否 | 600 | float 60..3600 | L1 循环缓冲时间窗（5-10 分钟） |
| memory.l1.max_entries | 否 | 500 | int 50..2000 | L1 循环缓冲容量 |
| memory.l1.max_content_chars | 否 | 300 | int 50..1000 | L1 单条文本化截断字数 |
| memory.compression.min_chars | 否 | 3000 | int 1000..10000 | L1→L2 压缩触发阈值（累计文本量） |
| memory.compression.max_chars | 否 | 5000 | int 1000..20000 | L1→L2 强制压缩阈值 |
| memory.compression.l2_summary_min | 否 | 500 | int 200..2000 | L2 每段摘要目标字数下限 |
| memory.compression.l2_summary_max | 否 | 1000 | int 200..4000 | L2 每段摘要目标字数上限 |
| memory.compression.l3_entry_max | 否 | 300 | int 100..1000 | L2→L3 归档后每段字数上限 |
| memory.compression.model | 否 | deepseek-v4-flash | str | 压缩模型（禁用 glm-4.7-flash） |
| memory.strength.low/medium/high/ultra | 否 | 2/5/10/15 | int 1..50 | 记忆强度→检索条数 k 映射 |

## 3. 输入契约
- 输入格式：`MemoryConfig(config_loader=None)`
- config_loader：ConfigLoader 实例（可选；缺省全部走默认值）
- 输入格式：`k_for(strength: str) -> int`
- strength：low / medium / high / ultra（未知值回退默认档）

## 4. 输出契约
- 成功：实例属性可读各配置项；k_for 返回对应检索条数
- 失败：不抛异常；未知 strength 回退 medium 档
- 事件：无

## 5. 依赖声明
- 外部服务：无
- 内部模块：src.shared.config_loader（可选，构造注入）
- 预先配置：config/config.yaml 的 memory 段（可选）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 无 | 配置读取为 get() 兜底，不抛异常 | — |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| k_for(strength) | 是 | 记忆强度 → 检索条数 k |

## 8. 领域状态说明
- 状态项：各配置项为构造期只读快照
- 持久化：无（配置来自 config.yaml，运行期不修改）
- 恢复：重启后重新读取配置
"""
from typing import Optional

from src.shared.config_loader import ConfigLoader


class MemoryConfig:
    """分层记忆配置快照（只读）。"""

    # 记忆强度 → 检索条数 k（规格书 4.3：低 2 / 中 5 / 高 10 / 超强 15）
    STRENGTH_K = {"low": 2, "medium": 5, "high": 10, "ultra": 15}
    DEFAULT_STRENGTH = "medium"
    # 压缩模型：统一 deepseek-v4-flash（规格书 4.2：禁用 glm-4.7-flash）
    COMPRESS_MODEL = "deepseek-v4-flash"

    def __init__(self, config_loader: Optional[ConfigLoader] = None):
        loader = config_loader
        get = loader.get if loader is not None else self._no_config

        self.l0_retention_days = int(get("memory.l0_retention_days", 14))
        self.l1_window_sec = float(get("memory.l1.window_sec", 600.0))
        self.l1_max_entries = int(get("memory.l1.max_entries", 500))
        self.l1_max_content_chars = int(get("memory.l1.max_content_chars", 300))
        self.compress_min_chars = int(get("memory.compression.min_chars", 3000))
        self.compress_max_chars = int(get("memory.compression.max_chars", 5000))
        self.l2_summary_min = int(get("memory.compression.l2_summary_min", 500))
        self.l2_summary_max = int(get("memory.compression.l2_summary_max", 1000))
        self.l3_entry_max = int(get("memory.compression.l3_entry_max", 300))
        self.compress_model = str(get("memory.compression.model", self.COMPRESS_MODEL))
        self.strength_default = str(get("memory.strength_default", self.DEFAULT_STRENGTH))
        # strength 映射：逐档读取，支持部分覆盖
        self.strength_k: dict = dict(self.STRENGTH_K)
        for key in self.STRENGTH_K:
            value = get(f"memory.strength.{key}", self.STRENGTH_K[key])
            self.strength_k[key] = int(value)

    @staticmethod
    def _no_config(key: str, default):
        return default

    def k_for(self, strength: Optional[str] = None) -> int:
        """记忆强度 → 检索条数 k；未知档回退默认档。"""
        key = (strength or self.strength_default or self.DEFAULT_STRENGTH).lower()
        if key not in self.strength_k:
            key = self.DEFAULT_STRENGTH
        return self.strength_k[key]
