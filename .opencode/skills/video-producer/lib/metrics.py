"""画面质量与视觉统计指标。

全部基于 numpy + PIL，避免引入 OpenCV 依赖，保证在干净环境可复现。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter


def load_rgb(path: Path | str, max_width: int | None = None) -> np.ndarray:
    img = Image.open(path).convert("RGB")
    if max_width and img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, max(1, round(img.height * ratio))), Image.LANCZOS)
    return np.asarray(img, dtype=np.float32)


def sharpness(arr: np.ndarray) -> float:
    """拉普拉斯方差：越高越清晰。对同一路素材横向可比。"""
    gray = arr.mean(axis=2)
    k = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float32)
    h, w = gray.shape
    if h < 3 or w < 3:
        return 0.0
    win = np.lib.stride_tricks.sliding_window_view(gray, (3, 3))
    lap = np.einsum("ijkl,kl->ij", win, k)
    return float(lap.var())


def brightness(arr: np.ndarray) -> float:
    return float(arr.mean() / 255.0)


def saturation(arr: np.ndarray) -> float:
    hsv = np.asarray(Image.fromarray(arr.astype(np.uint8)).convert("HSV"), dtype=np.float32)
    return float(hsv[:, :, 1].mean() / 255.0)


def color_cast(arr: np.ndarray) -> dict:
    """白平衡偏移：各通道均值相对灰度的比例，用于描述调色倾向。"""
    means = arr.reshape(-1, 3).mean(axis=0)
    gray = float(means.mean()) or 1.0
    return {
        "r": round(float(means[0] / gray), 4),
        "g": round(float(means[1] / gray), 4),
        "b": round(float(means[2] / gray), 4),
        "meanRgb": [round(float(x), 1) for x in means],
    }


def diff(a: np.ndarray, b: np.ndarray) -> float:
    """两张同尺寸图（0-255）的平均绝对差，归一化到 0-1。"""
    if a.shape != b.shape:
        small = (min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1]))
        a = a[: small[0], : small[1]]
        b = b[: small[0], : small[1]]
    return float(np.abs(a - b).mean() / 255.0)


def edge_row_profile(arr: np.ndarray) -> np.ndarray:
    """逐行边缘强度，用于定位字幕带。"""
    gray = Image.fromarray(arr.astype(np.uint8)).convert("L").filter(ImageFilter.FIND_EDGES)
    g = np.asarray(gray, dtype=np.float32)
    return g.mean(axis=1)


def text_band(arr: np.ndarray, *, lower_from: float = 0.45, lower_to: float = 0.95,
              band_ratio: tuple[float, float] = (0.02, 0.12)) -> dict:
    """在画面下半部定位最像字幕的水平带。

    依据：字幕行边缘密集且横向铺展在中部，且通常只有几行高。
    返回相对坐标（0-1），避免绑定具体分辨率。
    """
    h, w = arr.shape[:2]
    profile = edge_row_profile(arr)
    lo, hi = int(h * lower_from), int(h * lower_to)
    if hi <= lo:
        lo, hi = int(h * 0.5), h

    best = {"score": -1.0, "top": 0.75, "bottom": 0.80}
    min_h = max(4, int(h * band_ratio[0]))
    max_h = max(min_h + 1, int(h * band_ratio[1]))
    for band_h in range(min_h, max_h + 1, 2):
        for top in range(lo, max(lo, hi - band_h)):
            score = float(profile[top: top + band_h].mean())
            if score > best["score"]:
                best = {"score": score, "top": top, "bottom": top + band_h}
    return {
        "y": round(best["top"] / h, 4),
        "height": round((best["bottom"] - best["top"]) / h, 4),
        "edgeScore": round(best["score"], 2),
    }


def crop_band(arr: np.ndarray, band: dict, pad: float = 0.004) -> np.ndarray:
    h, w = arr.shape[:2]
    top = int(max(0, (band["y"] - pad)) * h)
    bottom = int(min(1.0, (band["y"] + band["height"] + pad)) * h)
    return arr[top:bottom, :]


def column_energy(arr: np.ndarray) -> np.ndarray:
    """逐列边缘强度，用于判断画面细节集中在哪里。"""
    gray = Image.fromarray(arr.astype(np.uint8)).convert("L").filter(ImageFilter.FIND_EDGES)
    return np.asarray(gray, dtype=np.float32).mean(axis=0)


def best_crop_x(arr: np.ndarray, crop_w: int) -> int:
    """在允许范围内选一个让主体尽量居中的裁切横坐标。

    以列边缘能量为权重做滑窗积分，能量最高处即细节最密集处（产品本体）。
    结果会按画宽比例收敛，避免为了追细节把画面推到最边上。
    """
    h, w = arr.shape[:2]
    if crop_w >= w:
        return 0
    crop_w = max(2, min(crop_w, w))

    prof = column_energy(arr)
    smooth = max(3, crop_w // 24)
    kernel = np.ones(smooth, dtype=np.float32) / smooth
    prof = np.convolve(prof, kernel, mode="same")

    csum = np.cumsum(np.insert(prof, 0, 0))
    window = csum[crop_w:] - csum[:-crop_w]
    x = float(np.argmax(window))

    # 限制偏移量：裁切框中心不越过画宽的 ±18%，画面更稳
    cx_center = x + crop_w / 2
    max_shift = w * 0.18
    delta = max(-max_shift, min(max_shift, cx_center - w / 2))
    x = int(round(w / 2 + delta - crop_w / 2))
    return max(0, min(w - crop_w, x))


def white_text_ratio(arr: np.ndarray) -> float:
    """近白像素占比，用于判断字幕主色是否为白色。"""
    px = arr.reshape(-1, 3)
    white = (px.min(axis=1) > 200) & (np.ptp(px, axis=1) < 40)
    return float(white.mean())
