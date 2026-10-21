#!/usr/bin/env python3
"""analyze_reference.py — 拆解参考成品，产出 style-spec.json

拆什么：
  1. 节奏：镜头边界、平均镜长、节拍
  2. 内容：每个镜头代表帧 + 本地视觉模型描述 + 构图类型
  3. 文案：字幕带定位、逐句文字与起止时间
  4. 视觉：色调偏移、饱和度、亮度
  5. 声音：响度、节拍、是否有配音/配乐

产出：
  work/reference/style-spec.json   供后续编排使用的结构化风格定义
  work/reference/拆解报告.md        人读版

用法：
  python scripts/analyze_reference.py --input 参考视频/reference.mp4
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from PIL import Image

from lib import ffmpeg, metrics, project, vlm
from lib.run import Step, log, read_json, write_json

CACHE_PATH = project.WORK / "cache" / "vlm_cache.json"


class Cache:
    """VLM 结果缓存：同一张图的同一类问题只花一次推理成本。"""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, str] = {}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        self.dirty = False

    @staticmethod
    def key(image: Path, kind: str) -> str:
        digest = hashlib.sha1(Path(image).read_bytes()).hexdigest()[:16]
        return f"{kind}:{digest}"

    def get(self, image: Path, kind: str) -> str | None:
        return self.data.get(self.key(image, kind))

    def put(self, image: Path, kind: str, value: str) -> None:
        self.data[self.key(image, kind)] = value
        self.dirty = True

    def flush(self) -> None:
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        self.dirty = False


def shot_boundaries(duration: float, cuts: list[float]) -> list[dict]:
    marks = [0.0] + [c for c in cuts if 0.3 < c < duration - 0.3] + [duration]
    shots = []
    for i in range(len(marks) - 1):
        start, end = marks[i], marks[i + 1]
        if end - start < 0.25:
            continue
        shots.append({"index": len(shots) + 1, "start": round(start, 3), "end": round(end, 3)})
    for s in shots:
        s["duration"] = round(s["end"] - s["start"], 3)
    return shots


def detect_caption_band(frames: list[Path]) -> dict:
    """多帧投票，取中位数，避免被单帧画面干扰。"""
    picks = frames[:: max(1, len(frames) // 12)][:12]
    bands = []
    for f in picks:
        arr = metrics.load_rgb(f, max_width=720)
        b = metrics.text_band(arr)
        if b["edgeScore"] > 0:
            bands.append(b)
    if not bands:
        return {"y": 0.78, "height": 0.035, "edgeScore": 0.0}
    ys = sorted(b["y"] for b in bands)
    hs = sorted(b["height"] for b in bands)
    mid = len(bands) // 2
    return {"y": round(ys[mid], 4), "height": round(hs[mid], 4),
            "edgeScore": round(float(np.median([b["edgeScore"] for b in bands])), 2)}


def segment_captions(frames: list[Path], fps: float, band: dict, cache: Cache,
                     out_dir: Path) -> list[dict]:
    """逐帧比对字幕带，只在内容变化时做一次 OCR。

    OCR 只传字幕带裁切图（不是整帧）：既避免把画面其他位置的文字混进来，
    也把单次推理成本降到最低。
    """
    band_dir = out_dir / "caption_bands"
    band_dir.mkdir(parents=True, exist_ok=True)

    timeline: list[dict] = []
    prev_crop: np.ndarray | None = None
    prev_text: str | None = None
    pending_start = 0.0

    for i, f in enumerate(frames):
        t = round(i / fps, 3)
        arr = metrics.load_rgb(f)
        crop = metrics.crop_band(arr, band)

        changed = prev_crop is None or metrics.diff(prev_crop, crop) > 0.012
        prev_crop = crop
        if not changed:
            continue

        # 没有近白像素基本可判定该帧无字幕，直接跳过推理
        if metrics.white_text_ratio(crop) < 0.012:
            text = ""
        else:
            crop_path = band_dir / f"band_{i:05d}.png"
            Image.fromarray(crop.astype(np.uint8)).save(crop_path)
            text = cache.get(crop_path, "ocr")
            if text is None:
                text = vlm.ocr(crop_path)
                cache.put(crop_path, "ocr", text)
            crop_path.unlink(missing_ok=True)

        similar = (
            prev_text is not None
            and difflib.SequenceMatcher(None, prev_text, text).ratio() > 0.72
        )
        if similar:
            continue

        if prev_text:
            timeline.append({"start": round(pending_start, 3), "end": t, "text": prev_text})

        prev_text = text
        pending_start = t

    if prev_text:
        timeline.append({"start": round(pending_start, 3), "end": None, "text": prev_text})

    clean = [s for s in timeline if s["text"]]
    for idx, seg in enumerate(clean):
        if seg["end"] is None:
            seg["end"] = clean[idx + 1]["start"] if idx + 1 < len(clean) else None
        if seg["end"] is not None:
            seg["duration"] = round(seg["end"] - seg["start"], 3)
    return clean


def detect_disclaimer(frames: list[Path], cache: Cache, out_dir: Path) -> dict | None:
    """检测画面固定位置的免责声明（如"本视频由AI配音 非真人发声"）。

    做法：取顶部条带做多帧 OCR，去掉重复后若稳定存在则认定为固定角标。
    """
    if not frames:
        return None
    picks = [frames[int(len(frames) * r)] for r in (0.15, 0.45, 0.75)]
    texts: list[str] = []
    tmp = out_dir / "disclaimer.png"
    for f in picks:
        arr = metrics.load_rgb(f)
        h, w = arr.shape[:2]
        strip = arr[int(h * 0.02): int(h * 0.11), int(w * 0.35): w]
        Image.fromarray(strip.astype(np.uint8)).save(tmp)
        key = cache.get(tmp, "ocr")
        text = key if key is not None else vlm.ocr(tmp)
        if key is None:
            cache.put(tmp, "ocr", text)
        if text:
            texts.append(text)
    tmp.unlink(missing_ok=True)
    if not texts:
        return None
    best = max(texts, key=len)
    support = sum(1 for t in texts if difflib.SequenceMatcher(None, best, t).ratio() > 0.7)
    if support < 2:
        return None
    return {"text": best, "position": "top-right", "support": support}


def analyze_audio(path: Path, out_dir: Path) -> dict:
    import librosa
    import warnings

    warnings.filterwarnings("ignore")
    wav = ffmpeg.extract_audio(path, out_dir / "audio.wav", sr=22050, mono=True)
    y, sr = librosa.load(str(wav), sr=22050, mono=True)

    tempo, beats = librosa.beat.beat_track(y=y, sr=sr)
    yh, yp = librosa.effects.hpss(y)
    perc_ratio = float(np.mean(yp ** 2) / (np.mean(y ** 2) + 1e-12))

    # 非静音区间占比，粗略反映"有没有持续人声"
    intervals = librosa.effects.split(y, top_db=32)
    speech_sec = sum((e - s) / sr for s, e in intervals)

    loud = ffmpeg.loudness(wav)
    return {
        "durationSec": round(len(y) / sr, 3),
        "integratedLufs": round(loud.get("input_i", 0.0), 2),
        "truePeakDb": round(loud.get("input_tp", 0.0), 2),
        "tempoBpm": round(float(np.atleast_1d(tempo)[0]), 1),
        "beatCount": int(len(beats)),
        "percussiveRatio": round(perc_ratio, 3),
        "activeRatio": round(speech_sec / max(len(y) / sr, 1e-6), 3),
        "hasVoiceover": bool(speech_sec / max(len(y) / sr, 1e-6) > 0.5),
        "hasMusic": bool(perc_ratio > 0.3 and len(beats) > 8),
    }


def pick_template(shots: list[dict]) -> dict:
    """从镜头序列里提炼可复用的叙事骨架（启发式，供 agent 进一步润色）。"""
    n = len(shots)
    if n == 0:
        return {"phases": []}
    kinds = [s["kinds"] for s in shots]

    first_wear = next((i for i, k in enumerate(kinds) if "model_wear" in k), n)
    last_wear = next((n - 1 - i for i, k in enumerate(reversed(kinds)) if "model_wear" in k), 0)

    phases = []
    if first_wear > 0:
        phases.append({
            "name": "hook",
            "startShot": 1,
            "endShot": first_wear,
            "intent": "用产品特写/手持快速抓注意力，配合痛点式提问",
        })
    if last_wear > first_wear:
        phases.append({
            "name": "proof",
            "startShot": first_wear + 1,
            "endShot": last_wear,
            "intent": "真人上身效果，建立信任与氛围感",
        })
    if last_wear < n:
        phases.append({
            "name": "close",
            "startShot": last_wear + 1,
            "endShot": n,
            "intent": "回到产品细节/对比，收束并引导下单",
        })
    return {"phases": phases}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=str(project.REFERENCE_DIR / "reference.mp4"))
    ap.add_argument("--out", default=str(project.WORK / "reference"))
    ap.add_argument("--scene-threshold", type=float, default=0.25)
    ap.add_argument("--caption-fps", type=float, default=5.0)
    ap.add_argument("--skip-vlm", action="store_true", help="只做机械分析，跳过视觉模型")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        raise SystemExit(f"找不到参考片：{src}\n先运行 scripts/fetch_reference.mjs 抓取。")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = Cache(CACHE_PATH)

    with Step("reference/summary") as s:
        info = ffmpeg.summary(src)
        log(s.name, f"{info['width']}x{info['height']} {info['fps']}fps {info['duration']:.2f}s")

    with Step("reference/shots") as s:
        cuts = ffmpeg.scene_cuts_stderr(src, args.scene_threshold)
        shots = shot_boundaries(info["duration"], cuts)
        log(s.name, f"{len(shots)} 个镜头，平均 {info['duration']/len(shots):.2f}s")

    with Step("reference/frames") as s:
        samples = ffmpeg.extract_frames(src, out_dir / "samples",
                                        fps=args.caption_fps, width=min(info["width"], 720))
        log(s.name, f"抽帧 {len(samples)} 张 @ {args.caption_fps}fps")

    with Step("reference/captions") as s:
        band = detect_caption_band(samples)
        log(s.name, f"字幕带 y={band['y']:.3f} h={band['height']:.3f}")
        if args.skip_vlm:
            # 字幕文案只用于参考，不影响出片；没有视觉模型时跳过，节奏与字幕位置仍可用
            captions = []
            log(s.name, "跳过字幕识别（--skip-vlm），字幕位置与节奏仍已测出", level="warn")
        else:
            captions = segment_captions(samples, args.caption_fps, band, cache, out_dir)
        disclaimer = detect_disclaimer(samples, cache, out_dir) if not args.skip_vlm else None
        if captions and captions[-1]["end"] is None:
            captions[-1]["end"] = round(info["duration"], 3)
            captions[-1]["duration"] = round(captions[-1]["end"] - captions[-1]["start"], 3)
        cache.flush()
        log(s.name, f"识别出 {len(captions)} 段文案"
                    + (f"，检测到固定角标：{disclaimer['text']}" if disclaimer else ""))

    with Step("reference/meta-shots") as s:
        trims = []
        for shot in shots:
            t = (shot["start"] + shot["end"]) / 2
            frame = ffmpeg.frame_at(src, t, out_dir / "shots" / f"shot_{shot['index']:03d}.jpg",
                                    width=min(info["width"], 540))
            arr = metrics.load_rgb(frame)
            shot["frame"] = str(frame.relative_to(project.ROOT))
            shot["color"] = metrics.color_cast(arr)
            shot["brightness"] = round(metrics.brightness(arr), 3)
            shot["saturation"] = round(metrics.saturation(arr), 3)
            shot["sharpness"] = round(metrics.sharpness(arr), 1)
            if args.skip_vlm:
                shot["description"] = ""
                shot["kinds"] = []
            else:
                desc = cache.get(frame, "describe")
                if desc is None:
                    desc = vlm.describe(frame)
                    cache.put(frame, "describe", desc)
                kind = cache.get(frame, "classify")
                if kind is None:
                    kind = vlm.classify(frame)
                    cache.put(frame, "classify", kind)
                shot["description"] = desc
                shot["kinds"] = [kind]
            trims.append(shot["duration"])
        cache.flush()
        log(s.name, f"镜头描述完成（VLM {'已跳过' if args.skip_vlm else '已调用'}）")

    with Step("reference/audio") as s:
        try:
            audio = analyze_audio(src, out_dir)
            log(s.name, f"响度 {audio['integratedLufs']}LUFS, 节拍 {audio['tempoBpm']}BPM")
        except Exception as exc:  # noqa: BLE001
            audio = {"error": str(exc)}
            log(s.name, f"音频分析失败：{exc}", level="warn")

    trims_sorted = sorted(trims)
    spec = {
        "schemaVersion": 1,
        "source": {
            "file": str(src.relative_to(project.ROOT)) if src.is_relative_to(project.ROOT) else str(src),
            "analyzedAt": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        },
        "canvas": {
            "width": info["width"],
            "height": info["height"],
            "fps": info["fps"],
            "aspect": f"{info['width']}:{info['height']}",
        },
        "rhythm": {
            "durationSec": round(info["duration"], 3),
            "shotCount": len(shots),
            "avgShotSec": round(info["duration"] / len(shots), 3),
            "shotSecP10": round(trims_sorted[max(0, int(len(trims_sorted) * 0.1) - 1)], 3),
            "shotSecP50": round(trims_sorted[len(trims_sorted) // 2], 3),
            "shotSecP90": round(trims_sorted[min(len(trims_sorted) - 1, int(len(trims_sorted) * 0.9))], 3),
            "cuts": cuts,
        },
        "shots": shots,
        "captions": {
            "band": band,
            "fontSizeRatio": round(band["height"] / 0.62, 4),
            "colorMode": "white_on_dark_outline",
            "align": "center",
            "maxCharsPerLine": max((len(s["text"]) for s in captions), default=0),
            "segments": captions,
            "firstText": captions[0]["text"] if captions else None,
        },
        "disclaimer": disclaimer,
        "audio": audio,
        "template": pick_template(shots),
    }

    write_json(out_dir / "style-spec.json", spec)
    write_json(out_dir / "meta.json", info)
    log("reference", f"已写出 {out_dir / 'style-spec.json'}")


if __name__ == "__main__":
    main()
