"""画面指纹：用来判断两个镜头是不是"看起来在讲同一件事"。

设计目标是抗压缩、抗轻微位移，但能区分"构图/配色不同"的镜头：
  - 颜色直方图（8×8×8）抓住商品配色和背景色调
  - 4×4 灰度网格抓住主体位置和构图
两者归一化后拼接做余弦相似度。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

HIST_BINS = 8
HIST_WEIGHT = 0.62


def signature(path: Path | str) -> np.ndarray:
    with Image.open(path) as im:
        rgb = im.convert("RGB").resize((64, 64), Image.LANCZOS)
        return from_array(np.asarray(rgb, dtype=np.float32))


def from_array(rgb: np.ndarray) -> np.ndarray:
    """从已解码的 RGB 数组算指纹（分析阶段可直接复用内存里的帧）。"""
    if rgb.dtype != np.float32:
        rgb = rgb.astype(np.float32)
    if rgb.max() > 1.5:
        rgb = rgb / 255.0
    if rgb.shape[:2] != (64, 64):
        rgb = np.asarray(
            Image.fromarray((rgb * 255).astype(np.uint8)).resize((64, 64), Image.LANCZOS),
            dtype=np.float32,
        ) / 255.0

    hist, _ = np.histogramdd(
        rgb.reshape(-1, 3),
        bins=(HIST_BINS, HIST_BINS, HIST_BINS),
        range=((0, 1), (0, 1), (0, 1)),
    )
    hist = hist.ravel().astype(np.float32)
    hist /= hist.sum() + 1e-9

    gray = rgb.mean(axis=2)
    grid = gray.reshape(4, 16, 4, 16).mean(axis=(1, 3)).ravel().astype(np.float32)
    grid -= grid.mean()
    grid /= np.linalg.norm(grid) + 1e-9

    hist = np.sqrt(hist)          # 开方压缩长尾，避免单一主色压倒判断
    hist /= np.linalg.norm(hist) + 1e-9
    return np.concatenate([hist * HIST_WEIGHT, grid * (1 - HIST_WEIGHT)])


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.clip(np.dot(a, b), -1.0, 1.0))
