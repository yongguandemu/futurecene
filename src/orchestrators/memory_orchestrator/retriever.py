"""retriever.py — 记忆检索（关键词打分 + 向量语义检索）

关键词检索：按查询词在内容/tags 中的命中数打分排序。
向量检索：字符 n-gram 特征哈希嵌入 + numpy 余弦相似度（轻量无语义模型方案），
支持注入外部 embedding 函数；numpy 缺失时自动降级为关键词检索。

# 模块内容清单（8 项契约）
1. 模块身份标识：memory 调度官 · retriever · 能力 memory:retrieve 的检索实现
2. 配置契约：embed_dim(256) / ngram(1..3) / k 由调用方传入
3. 输入契约：keyword_retrieve(entries, query, k) / vector_retrieve(entries, query, k, embedding)
             / hybrid_retrieve(entries, query, k) / merge_results(short, long, k)
4. 输出契约：检索结果条目 list（按相关性降序，至多 k 条）
5. 依赖声明：numpy（可选，缺失时向量/混合降级为关键词）
6. 错误定义：embedding 调用异常 → 降级关键词；numpy 缺失 → 降级关键词
7. 生命周期方法：无状态纯函数（embeddings 可在调用期注入）
8. 领域状态说明：无持久状态；向量为调用期计算
"""
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:  # pragma: no cover - 降级
    np = None
    _HAS_NUMPY = False

_EMBED_DIM = 256
_NGRAM_RANGE = (1, 3)


def keyword_retrieve(entries: List[Dict[str, Any]], query: str, k: int = 5) -> List[Dict[str, Any]]:
    """从记忆条目中按关键词重叠度检索。"""
    terms = [t for t in (query or "").lower().split() if t]
    if not terms:
        return entries[:k]
    scored = []
    for entry in entries:
        haystack = f"{entry.get('content', '')} {entry.get('tags', [])}".lower()
        score = sum(1 for t in terms if t in haystack)
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:k]]


def _default_embed(text: str, dim: int = _EMBED_DIM) -> Optional[Any]:
    """字符 n-gram 特征哈希嵌入（无语义模型，纯本地）。返回 numpy 向量或 None。"""
    if not _HAS_NUMPY:
        return None
    vec = np.zeros(dim, dtype=np.float32)
    text = (text or "").lower()
    for n in range(_NGRAM_RANGE[0], _NGRAM_RANGE[1] + 1):
        for i in range(len(text) - n + 1):
            gram = text[i:i + n]
            h = hash(gram) & 0x7FFFFFFF
            idx = h % dim
            sign = 1.0 if (h & 1) else -1.0
            vec[idx] = vec[idx] + sign
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec


def vector_retrieve(entries: List[Dict[str, Any]], query: str, k: int = 5,
                    embedding: Optional[Callable[[str], Any]] = None) -> List[Dict[str, Any]]:
    """按语义向量相似度检索（余弦）。返回至多 k 条，空查询/无向量时返回前 k 条。"""
    if not _HAS_NUMPY:
        logger.warning("[Retriever] numpy 缺失，向量检索降级为关键词")
        return keyword_retrieve(entries, query, k)
    embed_fn = embedding or _default_embed
    try:
        qv = embed_fn(str(query or ""))
    except Exception:
        qv = None
    if qv is None:
        return entries[:k]
    scored = []
    for entry in entries:
        try:
            ev = embed_fn(f"{entry.get('content', '')} {entry.get('tags', [])}")
            if ev is None:
                continue
            denom = (np.linalg.norm(qv) * np.linalg.norm(ev)) or 1e-9
            sim = float(np.dot(qv, ev) / denom)
            scored.append((sim, entry))
        except Exception as e:
            logger.debug("[Retriever] 记忆向量化失败: %s", e)
            continue
    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:k]]


def hybrid_retrieve(entries: List[Dict[str, Any]], query: str, k: int = 5,
                    keyword_weight: float = 0.5,
                    embedding: Optional[Callable[[str], Any]] = None) -> List[Dict[str, Any]]:
    """关键词 + 向量混合打分（归一化后加权合并）。"""
    # 关键词打分
    terms = [t for t in (query or "").lower().split() if t]
    kw_score: Dict[str, float] = {}
    for entry in entries:
        mid = entry.get("memory_id") or entry.get("content") or id(entry)
        haystack = f"{entry.get('content', '')} {entry.get('tags', [])}".lower()
        kw_score[mid] = float(sum(1 for t in terms if t in haystack)) if terms else 0.0
    # 向量打分
    vec_rank = vector_retrieve(entries, query, k=len(entries), embedding=embedding)
    vec_score: Dict[str, float] = {}
    n = max(1, len(vec_rank))
    for i, entry in enumerate(vec_rank):
        mid = entry.get("memory_id") or entry.get("content") or id(entry)
        vec_score[mid] = 1.0 - (i / n)  # 排名越靠前分越高
    # 合并
    scored = []
    for entry in entries:
        mid = entry.get("memory_id") or entry.get("content") or id(entry)
        kw_norm = kw_score.get(mid, 0.0) / max(1, max(kw_score.values() or [0]))
        s = keyword_weight * kw_norm + (1 - keyword_weight) * vec_score.get(mid, 0.0)
        scored.append((s, entry))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:k]]


def merge_results(short_term: List[Dict[str, Any]], long_term: List[Dict[str, Any]],
                  k: int = 5) -> List[Dict[str, Any]]:
    """短期 + 长期合并去重，按时间新→旧。"""
    seen = set()
    merged = []
    for entry in (long_term + short_term):
        mid = entry.get("memory_id")
        if mid in seen:
            continue
        seen.add(mid)
        merged.append(entry)
    merged.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
    return merged[:k]