"""质量口径：语义命中率的统一算法。

qc.py 和 version.py 必须用同一套口径，否则同一版片子会报出两个数字，
客户看到会以为哪里出错了。
"""
from __future__ import annotations

# 文案里出现这些词，说明这句话对画面有具体诉求，计入分母
VISUAL_DEMAND_WORDS = (
    "前扣", "扣", "肩带", "杯垫", "底围", "边缘", "走线", "豹纹", "黑色", "棕色",
    "浅蓝", "裸色", "包装", "多款", "对比", "平铺", "三角杯", "空杯", "副乳",
)


def semantic_hit_rate(plan: dict) -> tuple[float | None, int]:
    """返回（命中率, 有画面诉求的句子数）。

    命中定义：该句名下任一镜头的 semanticScore > 0。
    """
    shots = plan.get("shots", [])
    lines = plan.get("lines", [])
    demanding = [l for l in lines if any(w in (l.get("text") or "") for w in VISUAL_DEMAND_WORDS)]
    if not demanding:
        return None, 0
    hits = 0
    for line in demanding:
        owned = [s for s in shots if s.get("lineIndex") == line.get("index")]
        if any((s.get("semanticScore") or 0) > 0 for s in owned):
            hits += 1
    return round(hits / len(demanding), 3), len(demanding)
