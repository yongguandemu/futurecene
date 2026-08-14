"""vision.py — 画面理解（规格书 5.4 screen:describe）

- ocr：tesseract 文字识别（pytesseract）；tesseract 缺失时返回空串（降级）。
- describe：GLM-4V-Flash 视觉描述（可选增强）；不可用时返回占位描述。

# 模块内容清单（8 项契约）
1. 模块身份标识：screen · vision · 能力 screen:describe（OCR + 视觉描述）层
2. 配置契约：api_key 视觉模型密钥（describe 参数）；model 默认 glm-4.6v-flashx（付费高速，可经 GLMVISION_MODEL 覆盖），失败降级 glm-4v-flash
3. 输入契约：ocr(image_path, lang) / describe(image_path, api_key, model)
4. 输出契约：OCR 文本字符串 / 画面描述字符串
5. 依赖声明：base64、logging、typing、pytesseract/PIL（可选）、openai（可选）
6. 错误定义：依赖缺失或调用失败时返回空串/占位描述并记录日志
7. 生命周期方法：无（模块级函数）
8. 领域状态说明：无（无状态）
"""
import base64
import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import pytesseract
    from PIL import Image
except ImportError:
    pytesseract = None
    Image = None


def ocr(image_path: str, lang: str = "chi_sim+eng") -> str:
    """OCR 识别图片文字。"""
    if pytesseract is None or Image is None:
        logger.warning("[Vision] pytesseract/PIL 未安装，OCR 不可用")
        return ""
    try:
        text = pytesseract.image_to_string(Image.open(image_path), lang=lang)
        return (text or "").strip()
    except Exception as e:
        logger.error("[Vision] OCR 失败: %s", e)
        return ""


def describe(image_path: str, api_key: str = "", model: str = "glm-4.6v") -> str:
    """GLM 视觉描述（复用智谱 OpenAI 兼容接口）。

    默认模型 glm-4.6v-flashx（轻量高速付费版），可用环境变量 GLMVISION_MODEL 覆盖；
    调用失败自动降级免费型号 glm-4v-flash；仍失败返回占位描述。
    """
    if not api_key:
        return "[画面描述不可用：未配置视觉模型 api_key]"
    models = [model or "glm-4.6v-flashx", "glm-4v-flash"]
    for m in models:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key,
                            base_url="https://open.bigmodel.cn/api/paas/v4/")
            with open(image_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            resp = client.chat.completions.create(
                model=m,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": "用一句话描述这张游戏画面"},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ]}],
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            logger.error("[Vision] 视觉描述 %s 失败: %s", m, e)
    return "[画面描述失败]"
