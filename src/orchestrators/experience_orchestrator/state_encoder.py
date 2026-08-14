"""state_encoder.py — 状态表征（游戏经验学习域）

零 LLM 结构化画面状态，供经验库相似检索。
指纹 = 分区域 dHash 拼接 + 颜色直方图量化；相似度 = 指纹逐段汉明距离 + scene_type 匹配优先。
numpy 缺失时降级为纯文本/数值相似（不依赖外部数组库）。

# 模块内容清单 — state_encoder

## 1. 模块身份标识
- 所属调度官：experience
- 能力名：experience:encode（间接）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| 无 | - | - | - | 纯静态函数 + 数据类，无实例配置 |

## 3. 输入契约
- 输入格式：`encode(frame, scene_type, text)` / `fingerprint(frame, text_ratio)` / `similarity(a, b)` / `GameState.to_dict()/from_dict()`
- frame：可选，numpy BGR 图像（None 或 numpy 缺失时指纹为空串）
- scene_type：str，场景类型（默认 "unknown"）
- text：str，画面文本
- text_ratio：float，文本区域占比（默认 0.3）
- a/b：GameState，待比较状态

## 4. 输出契约
- 成功：`encode()` 返回 GameState；`fingerprint()` 返回 str（指纹或空串）；`similarity()` 返回 float（0.0-1.0）
- 失败：`fingerprint()` 帧为空 / numpy 缺失 / 异常返回空串（走文本相似兜底）
- 事件：无

## 5. 依赖声明
- 外部服务：无
- 内部模块：`numpy`、`PIL`（均可选，缺失自动降级）
- 预先配置：无

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| numpy/PIL 缺失 | 未安装 | 指纹为空串，走 scene_type + 文本相似兜底 |
| 帧异常 | 空帧 / 形状非法 | 返回空串指纹，记录静默跳过 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start/stop | 否 | 无（纯函数 + 数据类） |

## 8. 领域状态说明
- 状态项：无（纯函数；GameState 为不可变数据载体）
- 持久化：无
- 恢复：无状态，随调随用
"""
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:  # pragma: no cover - 降级
    np = None
    _HAS_NUMPY = False

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:  # pragma: no cover - 降级
    Image = None
    _HAS_PIL = False


@dataclass
class GameState:
    """经验库中的状态表示。"""
    scene_type: str = "unknown"
    text: str = ""
    fingerprint: str = ""
    hud: dict = field(default_factory=dict)
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GameState":
        return cls(**{k: d.get(k, v) for k, v in cls.__dataclass_fields__.items()
                      if k in d})


class StateEncoder:
    """画面状态编码器：输入帧 + scene_type + text → GameState（含指纹）。"""

    @staticmethod
    def fingerprint(frame, text_ratio: float = 0.3) -> str:
        """计算画面指纹。frame 为空或 numpy 不可用时返回空串（走文本相似兜底）。"""
        if frame is None or not _HAS_NUMPY or not _HAS_PIL:
            return ""
        try:
            h, w = frame.shape[:2]
            th = int(h * text_ratio)
            text_region = frame[th:, :, :]
            center_region = frame[:th, :, :]
            return "{}|{}|{}".format(
                _dhash(text_region), _dhash(center_region), _hist_quant(frame))
        except Exception:
            return ""

    @staticmethod
    def similarity(a: "GameState", b: "GameState") -> float:
        """相似度：有指纹按指纹逐段比较；无指纹按 scene_type + 文本重合度兜底。"""
        if a.fingerprint and b.fingerprint:
            score = 0.0
            pa = a.fingerprint.split("|")
            pb = b.fingerprint.split("|")
            compared = 0
            for part_a, part_b in zip(pa, pb):
                if len(part_a) == len(part_b) and part_a:
                    score += 1.0 - sum(x != y for x, y in zip(part_a, part_b)) / len(part_a)
                    compared += 1
            # 按实际比较的指纹段数归一化（真实指纹为 3 段，测试桩可为 1 段）
            base = score / max(1, compared)
            if a.scene_type == b.scene_type:
                base = base * 0.8 + 0.2
            return base
        # 无指纹兜底：scene_type 相同 + 文本重合度
        base = 0.0
        if a.scene_type == b.scene_type:
            base += 0.5
        ta, tb = (a.text or "").strip(), (b.text or "").strip()
        if ta and tb and ta == tb:
            base += 0.5
        return base


def _dhash(img, hash_size: int = 8) -> str:
    gray = Image.fromarray(np.asarray(img)).convert("L").resize(
        (hash_size + 1, hash_size), Image.LANCZOS)
    arr = np.asarray(gray, dtype=np.int16)
    bits = (arr[:, :-1] > arr[:, 1:]).flatten()
    return "".join("1" if b else "0" for b in bits)


def _hist_quant(frame, bins: int = 4) -> str:
    h, w = frame.shape[:2]
    if h == 0 or w == 0:
        return ""
    small = frame[::max(1, h // 16), ::max(1, w // 16), :].reshape(-1, 3)
    idx = (np.clip(small.astype(int), 0, 255) // (256 // bins)).sum(axis=1)
    counts = np.bincount(idx, minlength=bins ** 3)
    top = counts.argsort()[-8:][::-1]
    return "".join("{:02x}".format(int(t)) for t in top)


def encode(frame=None, scene_type: str = "unknown", text: str = "") -> GameState:
    """便捷工厂：frame + scene_type + text → GameState。"""
    fp = StateEncoder.fingerprint(frame)
    return GameState(scene_type=scene_type, text=(text or "")[:120],
                     fingerprint=fp, timestamp=time.time())