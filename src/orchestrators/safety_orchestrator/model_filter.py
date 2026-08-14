"""model_filter.py — 安全模型推理（增强项，规格书 613 行）

真实字符级 TextCNN 推理（参考旧系统 core/safety_intent.py 逻辑，按指挥官-调度官规范重写）。
模型文件缺失 / torch 缺失 / 推理异常 时回退到关键词规则兜底（不误伤正常内容）。

# 模块内容清单（8 项契约）
1. 模块身份标识：safety 调度官 · model_filter · 能力 safety:model_check
2. 配置契约：model_dir（默认 PROJECT_ROOT/data/safety_model，缺省回退旧项目 LumiProject/data/safety_model）
3. 输入契约：check(text: str) -> Optional[Dict]
4. 输出契约：{"safe": bool, "score": float, "source": "model"|"fallback", "label": str,
              "reason": str}；模型不可用返回 None（由规则兜底）
5. 依赖声明：torch（可选）、numpy（可选）；缺失自动降级
6. 错误定义：模型加载失败/推理异常 → 记录日志并返回 safe=True 兜底，不抛给上层
7. 生命周期方法：__init__(model_dir) 内 _try_load()；available 属性查询
8. 领域状态说明：_model TextCNN 实例 + _label_map；加载成功后保持，重启需重新加载
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from src.shared.config_loader import PROJECT_ROOT

logger = logging.getLogger(__name__)

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    _HAS_TORCH = True
except Exception:  # pragma: no cover - torch 缺失降级
    torch = nn = F = None
    _HAS_TORCH = False

MODEL_DIR = PROJECT_ROOT / "data" / "safety_model"
# 旧项目模型目录候选（新项目未迁移模型文件时回退参考）。
# 沙箱环境旧项目被映射到 VMCache 路径，故探测多个候选。
LEGACY_MODEL_DIRS = [
    Path(r"D:\future scene\LumiProject\data\safety_model"),
    Path(r"D:\TRAE_SOLO_CN\VMCache\main-TRAE_SOLO-Yinli\drive\D\future scene\LumiProject\data\safety_model"),
]

DEFAULT_CONFIG = {
    "max_len": 32, "embed_dim": 64, "num_filters": 64,
    "filter_sizes": [2, 3, 4], "num_classes": 4,
}

# 标签：0-safe / 1-suspicious / 2-dangerous / 3-unknown
_SAFE_LABELS = {0, 3}   # safe 与 unknown(低置信) 一律放行
_BLOCK_LABELS = {1, 2}  # suspicious / dangerous


class _TextCNNModel(nn.Module):
    """字符级 TextCNN：embedding → 多尺寸卷积 → 池化拼接 → fc。"""

    def __init__(self, vocab_size: int, embed_dim: int = 64,
                 num_filters: int = 64, filter_sizes=(2, 3, 4),
                 num_classes: int = 4):
        super().__init__()
        self.embedding = nn.Embedding(vocab_size, embed_dim, padding_idx=0)
        self.convs = nn.ModuleList([
            nn.Conv1d(embed_dim, num_filters, k) for k in filter_sizes
        ])
        self.dropout = nn.Dropout(0.5)
        self.fc = nn.Linear(num_filters * len(filter_sizes), num_classes)

    def forward(self, x):
        emb = self.embedding(x).transpose(1, 2)          # (B, embed, L)
        pooled = [
            F.max_pool1d(F.relu(conv(emb)), conv(emb).size(-1)).squeeze(-1)
            for conv in self.convs
        ]
        cat = torch.cat(pooled, dim=1)                   # (B, filters*k)
        return self.fc(self.dropout(cat))


class ModelFilter:
    """安全模型推理（可选增强）。模型不可用/推理失败时返回 safe 兜底，不阻断链路。"""

    def __init__(self, model_dir: str = "", probe_legacy: bool = True,
                 probe_legacy_dirs=None):
        self._model_dir = Path(model_dir) if model_dir else MODEL_DIR
        self._model: Optional[Any] = None
        self._vocab: Dict[str, int] = {}
        self._label_map: Dict[int, str] = {}
        self._max_len = DEFAULT_CONFIG["max_len"]
        self._embed_dim = DEFAULT_CONFIG["embed_dim"]
        self._num_filters = DEFAULT_CONFIG["num_filters"]
        self._filter_sizes = list(DEFAULT_CONFIG["filter_sizes"])
        self._num_classes = DEFAULT_CONFIG["num_classes"]
        self._loaded_from: Optional[Path] = None
        self._probe_legacy = probe_legacy
        # 探测目录：显式 model_dir 优先，其次旧项目候选（可注入/可关闭）
        self._probe_dirs = [self._model_dir]
        if self._probe_legacy:
            self._probe_dirs += list(probe_legacy_dirs or LEGACY_MODEL_DIRS)
        self._try_load()

    @property
    def available(self) -> bool:
        return self._model is not None

    @property
    def loaded_from(self) -> Optional[str]:
        return str(self._loaded_from) if self._loaded_from else None

    def check(self, text: str) -> Optional[Dict[str, Any]]:
        """模型推理：返回 {"safe", "score", "source", "label", "reason"}。

        模型不可用返回 None（由关键词规则兜底）；推理异常按 safe 兜底放行。
        """
        if not self.available:
            return None
        try:
            label_id, confidence = self._predict(str(text or ""))
            label = self._label_map.get(label_id, "unknown")
            safe = label_id in _SAFE_LABELS
            return {
                "safe": safe,
                "score": round(confidence, 4),
                "source": "model",
                "label": label,
                "reason": f"意图模型判定: {label} (conf={round(confidence, 4)})",
            }
        except Exception as e:
            logger.warning("[ModelFilter] 推理异常，按 safe 兜底: %s", e)
            return {"safe": True, "score": 0.0, "source": "fallback",
                    "label": "unknown", "reason": f"推理异常兜底: {e}"}

    # ---------- 推理管线 ----------

    def _predict(self, text: str):
        if self._model is None:
            raise RuntimeError("模型未加载")
        tokens = self._tokenize(text)
        with torch.no_grad():
            logits = self._model(
                torch.tensor([tokens], dtype=torch.long))
            probs = torch.softmax(logits, dim=1)[0]
            label_id = int(torch.argmax(probs).item())
            confidence = float(probs[label_id].item())
        return label_id, confidence

    def _tokenize(self, text: str):
        """字符级分词：映射到 vocab，填充/截断到 max_len。"""
        unk = self._vocab.get("<UNK>", 1)
        pad = self._vocab.get("<PAD>", 0)
        ids = [self._vocab.get(ch, unk) for ch in text]
        if len(ids) > self._max_len:
            ids = ids[:self._max_len]
        else:
            ids += [pad] * (self._max_len - len(ids))
        return ids

    # ---------- 加载 ----------

    def _try_load(self) -> None:
        if not _HAS_TORCH:
            logger.info("[ModelFilter] torch 未安装，使用规则兜底")
            return
        for d in self._probe_dirs:
            if self._load_from_dir(d):
                return
        logger.info("[ModelFilter] 未找到安全模型文件，使用规则兜底 (checked: %s)",
                    [str(x) for x in self._probe_dirs])

    def _load_from_dir(self, d: Path) -> bool:
        if not d.is_dir():
            return False
        model_path = d / "model.pt"
        vocab_path = d / "vocab.json"
        labels_path = d / "labels.json"
        if not (model_path.exists() and vocab_path.exists() and labels_path.exists()):
            return False
        try:
            with open(vocab_path, "r", encoding="utf-8") as f:
                self._vocab = json.load(f)
            with open(labels_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._label_map = {int(k): v for k, v in raw.items()}
            cfg_path = d / "config.json"
            if cfg_path.exists():
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                self._max_len = int(cfg.get("max_len", self._max_len))
                self._embed_dim = int(cfg.get("embed_dim", self._embed_dim))
                self._num_filters = int(cfg.get("num_filters", self._num_filters))
                self._filter_sizes = [int(x) for x in cfg.get(
                    "filter_sizes", self._filter_sizes)]
                self._num_classes = int(cfg.get("num_classes", self._num_classes))
            model = _TextCNNModel(
                vocab_size=len(self._vocab), embed_dim=self._embed_dim,
                num_filters=self._num_filters, filter_sizes=self._filter_sizes,
                num_classes=self._num_classes)
            state = torch.load(model_path, map_location="cpu")
            model.load_state_dict(state)
            model.eval()
            self._model = model
            self._loaded_from = d
            logger.info("[ModelFilter] TextCNN 加载完成 (vocab=%d, classes=%d) from %s",
                        len(self._vocab), self._num_classes, d)
            return True
        except Exception as e:
            logger.warning("[ModelFilter] 模型加载失败 (%s)，回退下一个目录: %s", d, e)
            self._model = None
            self._label_map = {}
            self._vocab = {}
            return False