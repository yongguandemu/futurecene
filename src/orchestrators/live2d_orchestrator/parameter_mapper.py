"""parameter_mapper.py — 情绪/动作 → Live2D 参数值映射（任务三）

将情绪标签 + 动作映射为具体参数值（范围取中值并钳制到注册表范围）。
映射前检查参数在模型注册表中存在，未知参数跳过（跨模型兼容）。

# 模块内容清单 — parameter_mapper

## 1. 模块身份标识
- 所属调度官：live2d · parameter_mapper · 能力 live2d:emotion/params_update 的参数来源

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| 无 | - | - | - | 映射表为模块常量，registry 注入 |

## 3. 输入契约
- map(emotion, motion="idle", model="") -> Dict[str, float]

## 4. 输出契约
- 成功：{param_id: value}（值已钳制到注册表范围）；未知情绪/无映射 → {}

## 5. 依赖声明
- 外部服务：无
- 内部模块：parameter_registry（注入，检查参数存在性）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 无 | - | 参数不存在于模型 → 跳过 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | - | 无状态 |

## 8. 领域状态说明
- 状态项：无
- 持久化：无
"""
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# 情绪 → 参数目标值（映射值取 0..1 归一，实际按注册表范围钳制）
EMOTION_PARAMS: Dict[str, Dict[str, float]] = {
    "开心": {"ParamSmile": 0.8, "ParamMouthSmile": 0.8, "ParamEyeLOpen": 0.7},
    "难过": {"ParamAngleZ": 0.25, "ParamMouthForm": 0.3, "ParamEyeLOpen": 0.35, "ParamEyeROpen": 0.35},
    "惊讶": {"ParamEyeLOpen": 0.95, "ParamEyeROpen": 0.95, "ParamMouthOpenY": 0.6, "ParamMouthForm": 0.6},
    "害羞": {"ParamAngleX": 0.15, "ParamEyeLOpen": 0.3, "ParamMouthSmile": 0.4},
    "生气": {"ParamAngleZ": 0.3, "ParamMouthForm": 0.7, "ParamEyeLOpen": 0.85, "ParamEyeROpen": 0.85},
    "平静": {},
}
# 动作 → 参数目标值（幅度归一）
MOTION_PARAMS: Dict[str, Dict[str, float]] = {
    "wave": {"ParamAngleZ": 0.35},
    "nod": {"ParamAngleX": 0.25},
    "shake": {"ParamAngleZ": 0.2},
    "idle": {},
}


class ParameterMapper:
    """情绪 + 动作 → 具体参数值（钳制到模型注册表范围）。"""

    def __init__(self, registry=None):
        self._registry = registry

    def map(self, emotion: str, motion: str = "idle", model: str = "") -> Dict[str, float]:
        merged: Dict[str, float] = {}
        for source in (EMOTION_PARAMS.get(emotion or "平静", {}),
                       MOTION_PARAMS.get(motion or "idle", {})):
            for pid, target in source.items():
                value = self._resolve(pid, target, model)
                if value is not None:
                    merged[pid] = value
        return merged

    def _resolve(self, pid: str, target: float, model: str) -> Optional[float]:
        if self._registry is None:
            return round(target, 3)
        spec = self._registry.get(model, pid)
        if spec is None:
            return None  # 模型无此参数，跳过
        lo, hi = float(spec.get("min", 0.0)), float(spec.get("max", 1.0))
        value = lo + (hi - lo) * max(0.0, min(1.0, target))
        return round(value, 3)
