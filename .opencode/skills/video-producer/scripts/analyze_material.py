#!/usr/bin/env python3
"""analyze_material.py — 素材体检与切片

对每段原始素材：
  1. 探测规格，按画面突变切成"场景"
  2. 在场景内滑动窗口打分（清晰度、抖动、曝光、饱和度）
  3. 用本地视觉模型标注构图类型与内容描述
  4. 输出可直接被编排器消费的候选片段列表

产出：work/material/material-index.json

用法：
  python scripts/analyze_material.py --input 学习素材 --out work/material
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from lib import ffmpeg, metrics, project, sig, vlm
from lib.run import Step, log, write_json

CACHE_PATH = project.WORK / "cache" / "vlm_cache.json"
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm"}
IMAGE_EXT = {".heic", ".jpg", ".jpeg", ".png", ".heif"}

# 打分权重：清晰度为主，抖动重罚，过曝/欠曝轻罚
W_SHARPNESS = 1.0
W_MOTION = 2.2
W_EXPOSURE = 0.8


class Cache:
    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.dirty = False

    @staticmethod
    def key(image: Path, kind: str) -> str:
        return f"{kind}:{hashlib.sha1(Path(image).read_bytes()).hexdigest()[:16]}"

    def get(self, image: Path, kind: str) -> str | None:
        return self.data.get(self.key(image, kind))

    def put(self, image: Path, kind: str, value: str) -> None:
        self.data[self.key(image, kind)] = value
        self.dirty = True

    def flush(self) -> None:
        if self.dirty:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            self.dirty = False


def list_materials(root: Path) -> list[Path]:
    files = [p for p in root.rglob("*") if p.suffix.lower() in VIDEO_EXT | IMAGE_EXT and p.is_file()]
    return sorted(files, key=lambda p: p.name)


def score_windows(frames: list[Path], fps: float, info: dict, *, win: float, hop: float,
                  arrs: list[np.ndarray] | None = None) -> list[dict]:
    """在每个窗口上算质量分，返回候选片段。"""
    if arrs is None:
        arrs = [metrics.load_rgb(f, max_width=400) for f in frames]
    sharp = np.array([metrics.sharpness(a) for a in arrs], dtype=np.float32)
    bright = np.array([metrics.brightness(a) for a in arrs], dtype=np.float32)
    sat = np.array([metrics.saturation(a) for a in arrs], dtype=np.float32)
    motion = np.array([0.0] + [metrics.diff(arrs[i - 1], arrs[i]) for i in range(1, len(arrs))],
                      dtype=np.float32)

    # 归一化：清晰度用分位数缩放，避免不同设备尺度差异
    p90 = float(np.percentile(sharp, 90)) or 1.0
    sharp_n = np.clip(sharp / p90, 0, 1)

    n_win = max(1, int((len(frames) / fps - win) / hop) + 1)
    windows = []
    for k in range(n_win):
        s = k * hop
        i0 = int(s * fps)
        i1 = min(len(frames), int((s + win) * fps))
        if i1 - i0 < 2:
            break
        sl = slice(i0, i1)
        sharpness = float(sharp_n[sl].mean())
        m = float(motion[sl].mean())
        b = float(bright[sl].mean())
        exposure_penalty = abs(b - 0.52) * 2.0
        score = W_SHARPNESS * sharpness - W_MOTION * m - W_EXPOSURE * exposure_penalty
        windows.append({
            "start": round(s, 3),
            "end": round(s + win, 3),
            "duration": round(win, 3),
            "score": round(float(score), 4),
            "sharpness": round(sharpness, 3),
            "motion": round(m, 4),
            "brightness": round(b, 3),
            "saturation": round(float(sat[sl].mean()), 3),
        })
    return windows


def pick_non_overlapping(windows: list[dict], limit: int) -> list[dict]:
    """按分数从高到低挑选互不重叠的片段，保证同一素材不被反复用同一段。"""
    chosen: list[dict] = []
    for w in sorted(windows, key=lambda x: -x["score"]):
        if all(w["end"] <= c["start"] or w["start"] >= c["end"] for c in chosen):
            chosen.append(w)
        if len(chosen) >= limit:
            break
    return sorted(chosen, key=lambda x: x["start"])


def analyze_video(path: Path, out_dir: Path, cache: Cache, *, win: float, hop: float,
                  per_clip: int, use_vlm: bool, fast: bool = False) -> tuple[dict, dict]:
    info = ffmpeg.summary(path)
    stem = path.stem
    frames = ffmpeg.extract_frames(path, out_dir / "frames" / stem, fps=2.0, width=400, prefix=stem)
    arrs = [metrics.load_rgb(f, max_width=400) for f in frames]
    windows = score_windows(frames, 2.0, info, win=win, hop=hop, arrs=arrs)
    segments = pick_non_overlapping(windows, per_clip)

    # 每个候选片段算一枚画面指纹，供编排阶段做"不要重复展示"的约束
    signatures: dict[str, list[float]] = {}
    for seg in segments:
        idx = min(max(int(((seg["start"] + seg["end"]) / 2) * 2.0), 0), len(arrs) - 1)
        key = f"{stem}@{seg['start']}"
        signatures[key] = [round(float(x), 5) for x in sig.from_array(arrs[idx])]
        # 标注"主体 + 动作"，编排阶段靠它把画面和口播语义对齐
        if use_vlm and 0 <= idx < len(frames):
            shot_frame = frames[idx]
            label = cache.get(shot_frame, "label3")
            if label is None:
                label = vlm.label_segment(shot_frame)
                cache.put(shot_frame, "label3", label)
            seg["subject"], seg["action"], seg["details"] = vlm.split_label(label)

    mid = (info["duration"] / 2)
    rep = ffmpeg.frame_at(path, mid, out_dir / "reps" / f"{stem}.jpg", width=540)
    arr = metrics.load_rgb(rep)
    entry = {
        "id": stem,
        "path": str(path.relative_to(project.ROOT)) if path.is_relative_to(project.ROOT) else str(path),
        "duration": round(info["duration"], 3),
        "width": info["width"],
        "height": info["height"],
        "fps": info["fps"],
        "codec": info["videoCodec"],
        "representativeFrame": str(rep.relative_to(project.ROOT)),
        "color": metrics.color_cast(arr),
        "brightness": round(metrics.brightness(arr), 3),
        "saturation": round(metrics.saturation(arr), 3),
        "segments": segments,
    }
    if use_vlm and not fast:
        desc = cache.get(rep, "describe")
        kind = cache.get(rep, "classify")
        if desc is None:
            desc = vlm.describe(rep)
            cache.put(rep, "describe", desc)
        if kind is None:
            kind = vlm.classify(rep)
            cache.put(rep, "classify", kind)
        entry["description"] = desc
        entry["kinds"] = [kind]
    else:
        entry["description"] = ""
        entry["kinds"] = []

    # 清理分析用的抽帧，避免 work 目录膨胀
    for f in frames:
        f.unlink(missing_ok=True)
    return entry, signatures


def analyze_image(path: Path, out_dir: Path, cache: Cache, use_vlm: bool, fast: bool = False) -> dict:
    from PIL import Image

    try:  # HEIC 支持是可选的，缺了也不该让整条流水线挂掉
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass

    rep = out_dir / "reps" / f"{path.stem}.jpg"
    rep.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(path) as im:
        im.convert("RGB").save(rep, quality=92)
    arr = metrics.load_rgb(rep)
    entry = {
        "id": path.stem,
        # ffmpeg 不一定能解 HEIC，统一转成 JPG 作为可渲染源，原路径留档
        "path": str(rep.relative_to(project.ROOT)),
        "originalPath": str(path.relative_to(project.ROOT)) if path.is_relative_to(project.ROOT) else str(path),
        "duration": 0.0,
        "width": int(arr.shape[1]),
        "height": int(arr.shape[0]),
        "fps": 0.0,
        "codec": "image",
        "representativeFrame": str(rep.relative_to(project.ROOT)),
        "color": metrics.color_cast(arr),
        "brightness": round(metrics.brightness(arr), 3),
        "saturation": round(metrics.saturation(arr), 3),
        # 静态图默认给一个 1.2s 的可用片段，允许编排器做缓慢推拉
        "segments": [{"start": 0.0, "end": 1.2, "duration": 1.2, "score": 0.5,
                      "sharpness": 1.0, "motion": 0.0,
                      "brightness": round(metrics.brightness(arr), 3),
                      "saturation": round(metrics.saturation(arr), 3)}],
        "static": True,
    }
    if use_vlm and not fast:
        desc = cache.get(rep, "describe")
        kind = cache.get(rep, "classify")
        if desc is None:
            desc = vlm.describe(rep)
            cache.put(rep, "describe", desc)
        if kind is None:
            kind = vlm.classify(rep)
            cache.put(rep, "classify", kind)
        entry["description"] = desc
        entry["kinds"] = [kind]
        label = cache.get(rep, "label3")
        if label is None:
            label = vlm.label_segment(rep)
            cache.put(rep, "label3", label)
        entry["subject"], entry["action"], entry["details"] = vlm.split_label(label)
    else:
        entry["description"] = ""
        entry["kinds"] = []
        entry["subject"], entry["action"], entry["details"] = "", "", ""
    return entry


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=str(project.MATERIAL_DIR))
    ap.add_argument("--out", default=str(project.WORK / "material"))
    ap.add_argument("--window", type=float, default=1.6, help="候选片段时长（秒）")
    ap.add_argument("--hop", type=float, default=0.4)
    ap.add_argument("--per-clip", type=int, default=project.SEGMENTS_PER_CLIP,
                    help="每段素材最多保留几个候选片段")
    ap.add_argument("--skip-vlm", action="store_true")
    ap.add_argument("--fast", action="store_true", default=project.FAST_ANALYSIS,
                    help="快模式：跳过描述/构图分类，只保留主体动作标注，省约 1/4 推理时间")
    args = ap.parse_args()

    root = Path(args.input)
    if not root.is_dir():
        raise SystemExit(f"素材目录不存在：{root}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = Cache(CACHE_PATH)

    if not args.skip_vlm:
        health = vlm.health()
        if not health["ok"]:
            raise SystemExit(
                "本地视觉模型不可用，语义标注无法生成。\n"
                f"  目标：{project.VLM_MODEL} @ {project.VLM_URL}\n"
                f"  当前可用：{health.get('models') or health.get('error')}\n"
                f"  修复：bash {project.SKILL_DIR}/scripts/bootstrap.sh\n"
                "  或：ollama serve && ollama pull " + project.VLM_MODEL + "\n"
                "  确实要跳过视觉分析（会退化成按分数选镜）：加 --skip-vlm"
            )

    files = list_materials(root)
    if not files:
        raise SystemExit(f"素材目录里没有可用文件：{root}")

    clips = []
    signatures: dict[str, list[float]] = {}
    with Step("material") as s:
        for i, f in enumerate(files, 1):
            log(s.name, f"[{i}/{len(files)}] {f.name}")
            try:
                if f.suffix.lower() in IMAGE_EXT:
                    clips.append(analyze_image(f, out_dir, cache, not args.skip_vlm, fast=args.fast))
                else:
                    entry, sigs = analyze_video(f, out_dir, cache, win=args.window, hop=args.hop,
                                                per_clip=args.per_clip, use_vlm=not args.skip_vlm,
                                                fast=args.fast)
                    clips.append(entry)
                    signatures.update(sigs)
            except Exception as exc:  # noqa: BLE001
                log(s.name, f"{f.name} 分析失败：{exc}", level="warn")
        cache.flush()

    if not args.skip_vlm and len(clips) < len(files) * 0.8:
        raise SystemExit(
            f"只有 {len(clips)}/{len(files)} 个素材分析成功，超过 20% 失败。\n"
            "不要带着残缺的素材库往下走，先修失败原因（通常还是视觉模型或文件格式）。"
        )

    total_segments = sum(len(c["segments"]) for c in clips)
    index = {
        "schemaVersion": 1,
        "source": {"dir": str(root), "fileCount": len(clips)},
        "stats": {
            "videoCount": sum(1 for c in clips if not c.get("static")),
            "imageCount": sum(1 for c in clips if c.get("static")),
            "totalDuration": round(sum(c["duration"] for c in clips), 2),
            "usableSegments": total_segments,
        },
        "clips": clips,
    }
    write_json(out_dir / "material-index.json", index)
    write_json(out_dir / "signatures.json", signatures)
    log("material", f"{len(clips)} 个素材，{total_segments} 个候选片段 → {out_dir/'material-index.json'}")


if __name__ == "__main__":
    main()
