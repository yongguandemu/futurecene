"""train_emotion_mapping.py — 文本→情绪→参数对应关系命中率训练

用户定义的"训练"= 命中率调优（非神经网络微调）：
对标注数据集（文本 + 期望情绪）评测规则词典 / ONNX 模型的命中率，
并可从"模型正确但规则错误"的样本中学习新词，更新外部词典 lexicon.json，
使文本→情绪→Live2D 参数整条链的命中率可量化、可迭代提升。

用法：
  python scripts/train_emotion_mapping.py                # 评测 data/training/emotion_dataset.csv
  python scripts/train_emotion_mapping.py --init         # 生成数据集模板（首次使用）
  python scripts/train_emotion_mapping.py --dataset X.csv   # 指定数据集文件
  python scripts/train_emotion_mapping.py --export-lexicon  # 学词典写入 data/models/emotion/lexicon.json
  python scripts/train_emotion_mapping.py --min-count 3     # 词典学习最少出现次数（默认 2）

输出：命中率报告打印 + data/cache/emotion_mapping_report.json

# 模块内容清单 — train_emotion_mapping（scripts 工具）

## 1. 模块身份标识
- 所属调度官：无（独立训练/评测工具，产出物供 live2d.emotion 消费）

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 说明 |
|--------|------|--------|------|
| --dataset | 否 | data/training/emotion_dataset.csv | 标注数据集（text,expected_emotion） |
| --init | 否 | - | 生成数据集模板后退出 |
| --export-lexicon | 否 | - | 学习词典并写 lexicon.json |
| --min-count | 否 | 2 | 候选词最少出现次数 |

## 3. 输入契约
- CSV 列：text,expected_emotion（expected ∈ 开心/难过/惊讶/害羞/生气/平静）

## 4. 输出契约
- 成功：打印命中率报告，退出码 0；数据集缺失/空 → 提示并退出 1

## 5. 依赖声明
- 外部服务：无
- 内部模块：src.orchestrators.live2d_orchestrator.emotion_extractor

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| 数据集缺失 | 文件不存在 | 先跑 --init 生成模板 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| main() | 是 | 一次性 CLI 入口 |

## 8. 领域状态说明
- 状态项：无
- 持久化：报告写 data/cache/，词典写 data/models/emotion/lexicon.json
- 恢复：可重复执行（幂等合并词典）
"""
import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestrators.live2d_orchestrator.emotion_extractor import (  # noqa: E402
    EMOTION_DIR,
    VALID_EMOTIONS,
    EmotionExtractor,
)

DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "data" / "training" / "emotion_dataset.csv"
REPORT_PATH = Path(__file__).resolve().parents[1] / "data" / "cache" / "emotion_mapping_report.json"
LEXICON_PATH = EMOTION_DIR / "lexicon.json"

TEMPLATE = """text,expected_emotion
今天好开心啊！,开心
气死我了,生气
什么？！竟然是这样,惊讶
呜呜呜好难过,难过
嗯，好的,平静
"""


def load_dataset(path: Path):
    """读 CSV → [(text, expected_emotion)]，容忍 BOM 与空行。"""
    if not path.exists():
        return None
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            text = (row.get("text") or "").strip()
            expected = (row.get("expected_emotion") or "").strip()
            if text and expected in VALID_EMOTIONS:
                rows.append((text, expected))
    return rows


def learn_lexicon(dataset, extractor, min_count: int):
    """从"模型正确但规则错误"的样本中提取 2-gram 候选词 → 词典增量。

    原理：规则未命中说明词库缺词；模型命中说明该文本情绪信号明确，
    其 2-gram 里大概率含可学习的情绪词。返回 (新增词, 各情绪计数)。
    """
    cand = defaultdict(Counter)   # emotion -> Counter(2gram)
    for text, expected in dataset:
        r = extractor._rule_extract(text)
        o = extractor._onnx_extract(text) if extractor._onnx_ready else None
        if o is None or o["emotion"] != expected:
            continue  # 模型未命中（或无模型）的样本不学词
        if r["emotion"] == expected:
            continue  # 规则已命中无需学
        # 去标点/空白后提取 2-gram
        chars = [c for c in text if c.strip() and c not in "！!？?….,。，、～~ "]
        for i in range(len(chars) - 1):
            gram = chars[i] + chars[i + 1]
            cand[expected][gram] += 1
    new_words = {e: [g for g, n in c.items() if n >= min_count]
                 for e, c in cand.items()}
    return {e: w for e, w in new_words.items() if w}


def export_lexicon(new_words: dict) -> None:
    """合并新增词到 lexicon.json（幂等，保留已有词与标点/语气扩展）。"""
    existing = {"words": {}, "punct": {}, "tone": {}}
    if LEXICON_PATH.exists():
        try:
            existing = json.loads(LEXICON_PATH.read_text(encoding="utf-8"))
        except Exception:
            existing = {"words": {}, "punct": {}, "tone": {}}
    words = dict(existing.get("words", {}))
    for emotion, grams in new_words.items():
        base = words.setdefault(emotion, [])
        for g in grams:
            if g not in base:
                base.append(g)
    payload = {"words": words, "punct": existing.get("punct", {}),
               "tone": existing.get("tone", {})}
    LEXICON_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEXICON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    print(f"[训练] 词典已写入 {LEXICON_PATH}")


def render_report(stats: dict) -> None:
    total = stats["total"]
    print("\n===== 文本→情绪 命中率报告 =====")
    print(f"数据集样本数: {total}")
    for key, label in (("rule", "规则词典"), ("onnx", "ONNX 模型")):
        s = stats[key]
        if total and (s["hit"] or s["total"]):
            print(f"\n[{label}] 命中 {s['hit']}/{total}  (命中率 {s['accuracy'] * 100:.1f}%)")
            for emo, v in s.get("per_emotion", {}).items():
                pct = v["hit"] / v["total"] * 100 if v["total"] else 0
                print(f"  {emo}: {v['hit']}/{v['total']} ({pct:.0f}%)")
    if stats.get("samples"):
        print(f"\n[错误样本] {len(stats['samples'])} 条（规则或模型至少其一未命中）：")
        for s in stats["samples"][:15]:
            print(f"  {s['text']!r} 期望={s['expected']} 规则={s['rule']} "
                  f"模型={s['onnx'] or '-'}")
        if len(stats["samples"]) > 15:
            print(f"  ... 其余 {len(stats['samples']) - 15} 条见报告文件")


def main() -> int:
    parser = argparse.ArgumentParser(description="文本→情绪→参数 命中率训练")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--init", action="store_true", help="生成数据集模板")
    parser.add_argument("--export-lexicon", action="store_true", help="学词典写 lexicon.json")
    parser.add_argument("--min-count", type=int, default=2, help="候选词最少出现次数")
    args = parser.parse_args()

    if args.init:
        DEFAULT_DATASET.parent.mkdir(parents=True, exist_ok=True)
        if DEFAULT_DATASET.exists():
            print(f"[模板] 已存在，跳过: {DEFAULT_DATASET}")
        else:
            DEFAULT_DATASET.write_text(TEMPLATE, encoding="utf-8")
            print(f"[模板] 已生成: {DEFAULT_DATASET}（text,expected_emotion）")
        return 0

    dataset = load_dataset(Path(args.dataset))
    if not dataset:
        print(f"[错误] 数据集不存在或为空: {args.dataset}（先跑 --init 生成模板）")
        return 1

    extractor = EmotionExtractor(source="auto")
    stats = extractor.evaluate(dataset)
    render_report(stats)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(f"\n[报告] 已保存 {REPORT_PATH}")

    if args.export_lexicon:
        new_words = learn_lexicon(dataset, extractor, args.min_count)
        total_new = sum(len(v) for v in new_words.values())
        if total_new == 0:
            print("[训练] 未学到新词（模型未就绪或规则已全部命中）")
        else:
            export_lexicon(new_words)
            for e, ws in new_words.items():
                print(f"  {e}: {ws}")
            print("[提示] 重启服务后 EmotionExtractor 自动加载新词典")
    return 0


if __name__ == "__main__":
    sys.exit(main())
