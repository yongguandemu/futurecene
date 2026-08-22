"""parameter_registry.py — Live2D 参数注册表（任务三）

模型加载时解析 data/models/<name>/<name>.model3.json 的 Parameters 段
（Id/Type/Min/Max/Default）并缓存；供 ParameterMapper 映射前校验参数存在性与范围。

# 模块内容清单 — parameter_registry

## 1. 模块身份标识
- 所属调度官：live2d · parameter_registry · 能力 live2d:params_update 的参数来源

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| models_dir | 否 | <项目根>/data/models | str | Live2D 模型目录 |

## 3. 输入契约
- load(model_name) -> Dict[str, Dict]；get(model_name, param_id) -> Optional[Dict]

## 4. 输出契约
- 成功：{param_id: {"min", "max", "default"}}；get 命中返回参数定义
- 失败：模型/文件缺失返回 {} / None（不抛异常）

## 5. 依赖声明
- 外部服务：无（解析本地 .model3.json）
- 内部模块：json、pathlib、typing、shared.config_loader（PROJECT_ROOT）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 文件缺失/解析失败 | model3.json 不存在或 JSON 损坏 | load 返回 {} 并记录警告 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | - | 构造即用，_cache 懒加载 |

## 8. 领域状态说明
- 状态项：_cache（model_name -> 参数表）、_models_dir
- 持久化：无（每次构造重新解析）
"""
import json
import logging
from pathlib import Path
from typing import Dict, Optional

from src.shared.config_loader import PROJECT_ROOT

logger = logging.getLogger(__name__)

MODELS_DIR = PROJECT_ROOT / "data" / "models"


class ParameterRegistry:
    """解析并缓存 Live2D 模型参数定义。"""

    def __init__(self, models_dir: str = ""):
        self._models_dir = Path(models_dir) if models_dir else MODELS_DIR
        self._cache: Dict[str, Dict[str, Dict[str, float]]] = {}

    def load(self, model_name: str) -> Dict[str, Dict[str, float]]:
        """解析模型参数表并缓存；文件缺失返回空表。"""
        if model_name in self._cache:
            return self._cache[model_name]
        model_path = self._find_model3(model_name)
        if model_path is None:
            logger.warning("[ParameterRegistry] 未找到模型 %s 的 model3.json", model_name)
            self._cache[model_name] = {}
            return self._cache[model_name]
        params: Dict[str, Dict[str, float]] = {}
        try:
            data = json.loads(model_path.read_text(encoding="utf-8"))
            for p in data.get("Parameters", []) or []:
                pid = p.get("Id")
                if not pid:
                    continue
                params[pid] = {
                    "min": float(p.get("Min", 0.0)),
                    "max": float(p.get("Max", 1.0)),
                    "default": float(p.get("Default", 0.0)),
                }
            self._cache[model_name] = params
            logger.info("[ParameterRegistry] %s 参数加载: %d 个", model_name, len(params))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("[ParameterRegistry] %s 解析失败: %s", model_name, e)
            self._cache[model_name] = {}
        return self._cache[model_name]

    def get(self, model_name: str, param_id: str) -> Optional[Dict[str, float]]:
        params = self.load(model_name)
        return params.get(param_id)

    def _find_model3(self, model_name: str) -> Optional[Path]:
        for cand in (self._models_dir / model_name / f"{model_name}.model3.json",
                     self._models_dir / f"{model_name}.model3.json",
                     self._models_dir / f"{model_name}.json"):
            if cand.exists():
                return cand
        return None
