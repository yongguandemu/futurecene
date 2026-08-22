"""timing_controller.py — 时序协调（任务三）

口型优先：说话期间抑制动作切换；非说话时段输出「身体轻微起伏（正弦）+ 周期性眨眼」，
呼吸功能禁用（不产生大幅呼吸参数）。供 orchestrator 周期性 tick 调用。

# 模块内容清单 — timing_controller

## 1. 模块身份标识
- 所属调度官：live2d · timing_controller · 能力 live2d:params_update 的 idle 时序来源

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| blink_interval | 否 | (3,5) | (float,float) | 眨眼周期秒范围 |
| body_amplitude | 否 | 0.08 | float | 身体起伏幅度（归一 0..1） |

## 3. 输入契约
- tick(now, speaking=False, lip_end_at=0.0) -> Dict[str, float]
- should_switch_motion(now, speaking) -> bool

## 4. 输出契约
- 成功：参数增量 dict（ParamEyeLOpen/ParamBodyAngleZ 等）；should_switch_motion 布尔
- 失败：无异常路径

## 5. 依赖声明
- 外部服务：无
- 内部模块：math、typing

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 无 | - | - |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | - | 无状态（纯函数式） |

## 8. 领域状态说明
- 状态项：无
- 持久化：无
"""
import math
import random
import time
from typing import Dict


class TimingController:
    """口型/眨眼/身体起伏时序协调（呼吸禁用）。"""

    def __init__(self, blink_interval=(3.0, 5.0), body_amplitude: float = 0.08):
        self._blink_interval = blink_interval
        self._body_amp = body_amplitude
        self._phase_offset = random.uniform(0.0, math.tau)

    def tick(self, now: float = None, speaking: bool = False,
             lip_end_at: float = 0.0) -> Dict[str, float]:
        """非说话时段：身体正弦起伏 + 周期性眨眼。返回参数增量。"""
        now = now if now is not None else time.time()
        params: Dict[str, float] = {}
        if not speaking:
            # 身体轻微起伏（呼吸禁用后的最小"活着"感）
            body = self._body_amp * math.sin(now * 0.6 + self._phase_offset)
            params["ParamBodyAngleZ"] = round(body, 4)
            # 周期性眨眼：眼开度 1 → 0 → 1（短促）
            lo, hi = self._blink_interval
            period = max(lo, min(hi, random.uniform(lo, hi)))
            phase = (now % period) / period
            if phase < 0.08:
                eye = round(max(0.0, 1.0 - phase / 0.08), 3)
                params["ParamEyeLOpen"] = eye
                params["ParamEyeROpen"] = eye
        return params

    @staticmethod
    def should_switch_motion(now: float, speaking: bool) -> bool:
        """说话期间不切换动作（口型优先）。"""
        return not speaking
