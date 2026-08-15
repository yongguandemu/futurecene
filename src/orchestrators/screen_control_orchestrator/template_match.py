"""template_match.py — 模板匹配识别（规格书 5.4 screen:template_match 扩展）

OpenCV 模板匹配：在截图中定位模板图像（按钮/图标/角色/物品等），
返回匹配位置与置信度。是 OCR 之外的第二条识别通道，用于无文字元素定位，
支撑「识别→判断→操作→反馈」闭环中更稳定的目标定位。

# 模块内容清单 — template_match

## 1. 模块身份标识
- 所属调度官：screen
- 能力名：screen:template_match（模板匹配识别）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| 无独立配置 | - | - | - | 阈值经调用参数传入 |

## 3. 输入契约
- 输入格式：`find_template(screenshot_path, template_path, threshold=0.8) -> Optional[Match]`
- Match：namedtuple(x, y, confidence)；x/y 为模板中心点在截图中的坐标
- `find_all_templates(...)` 返回全部匹配列表

## 4. 输出契约
- 成功：返回匹配（含中心坐标与置信度）；未命中返回 None / 空列表
- 失败：依赖缺失或图片异常 → 返回 None 并记录日志
- 事件：无

## 5. 依赖声明
- 外部服务：无（OpenCV 经 cv2，可选；缺失时降级为 None）
- 内部模块：logging、pathlib、typing
- 预先配置：无

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| cv2 未安装 | 依赖缺失 | 返回 None，调用方走 OCR/描述降级 |
| 图片读取失败 | 路径错误/损坏 | 返回 None 并记录日志 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | 否 | 纯函数式模块 |

## 8. 领域状态说明
- 状态项：无（无状态）
- 持久化：无
- 恢复：无
"""
import logging
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import cv2
    import numpy as np
    CV2_AVAILABLE = True
except ImportError:
    cv2 = None
    np = None
    CV2_AVAILABLE = False


class Match(NamedTuple):
    """模板匹配结果：模板中心在截图中的坐标 + 置信度。"""

    x: int
    y: int
    confidence: float


def available() -> bool:
    """OpenCV 是否可用。"""
    return CV2_AVAILABLE


def find_template(screenshot_path: str, template_path: str,
                  threshold: float = 0.8) -> Optional[Match]:
    """在截图中查找模板，返回最佳匹配（中心坐标 + 置信度）。

    Args:
        screenshot_path: 截图路径
        template_path: 模板图像路径
        threshold: 匹配阈值（0~1，越高越严格）
    """
    if not CV2_AVAILABLE:
        logger.warning("[TemplateMatch] OpenCV 未安装，模板匹配不可用")
        return None
    if not Path(screenshot_path).exists() or not Path(template_path).exists():
        logger.warning("[TemplateMatch] 截图或模板不存在: %s / %s",
                       screenshot_path, template_path)
        return None
    try:
        screen = cv2.imread(screenshot_path, cv2.IMREAD_COLOR)
        template = cv2.imread(template_path, cv2.IMREAD_COLOR)
        if screen is None or template is None:
            logger.warning("[TemplateMatch] 图片读取失败")
            return None
        sh, sw = screen.shape[:2]
        th, tw = template.shape[:2]
        if th > sh or tw > sw:
            logger.warning("[TemplateMatch] 模板大于截图，无法匹配")
            return None
        result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val < threshold:
            return None
        cx = int(max_loc[0] + tw / 2)
        cy = int(max_loc[1] + th / 2)
        logger.info("[TemplateMatch] 命中 %s @(%d,%d) conf=%.3f",
                    Path(template_path).name, cx, cy, max_val)
        return Match(cx, cy, float(max_val))
    except Exception as e:
        logger.error("[TemplateMatch] 匹配失败: %s", e)
        return None


def find_all_templates(screenshot_path: str, template_path: str,
                       threshold: float = 0.8,
                       max_results: int = 10) -> List[Match]:
    """查找全部匹配（非极大值抑制去重）。"""
    if not CV2_AVAILABLE:
        return []
    if not Path(screenshot_path).exists() or not Path(template_path).exists():
        return []
    try:
        screen = cv2.imread(screenshot_path, cv2.IMREAD_COLOR)
        template = cv2.imread(template_path, cv2.IMREAD_COLOR)
        if screen is None or template is None:
            return []
        sh, sw = screen.shape[:2]
        th, tw = template.shape[:2]
        if th > sh or tw > sw:
            return []
        result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
        loc = np.where(result >= threshold)
        matches: List[Match] = []
        for pt in zip(*loc[::-1]):
            matches.append(Match(int(pt[0] + tw / 2), int(pt[1] + th / 2),
                                 float(result[pt[1], pt[0]])))
        # 非极大值抑制：按置信度降序，抑制重叠区域
        matches.sort(key=lambda m: m.confidence, reverse=True)
        picked: List[Match] = []
        for m in matches:
            if all(abs(m.x - p.x) >= tw or abs(m.y - p.y) >= th for p in picked):
                picked.append(m)
            if len(picked) >= max_results:
                break
        return picked
    except Exception as e:
        logger.error("[TemplateMatch] 批量匹配失败: %s", e)
        return []
