#!/usr/bin/env python3
"""qc.py — 成片质检

不是"看起来没问题"就算过，逐项给数据：
  1. 规格：分辨率、帧率、时长、码率
  2. 响度：是否落在抖音推荐区间（-14 LUFS 左右）
  3. 黑帧/静音：有没有渲染事故
  4. 音画对齐：字幕条数与配音条数、总时长偏差
  5. 抽帧缩略图：给人眼快速复核

产出：work/qc/qc-report.json + work/qc/contact-sheet.jpg + 终端摘要
退出码：有阻断级问题时非 0，便于流水线中断。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import ffmpeg, project
from lib.quality import VISUAL_DEMAND_WORDS, semantic_hit_rate
from lib.run import Step, log, read_json, write_json

LUFS_RANGE = (-16.0, -12.0)

# 与 reference/quality-bar.md 保持一致；改这里必须同步改文档
QUALITY_BAR = {
    "durationSec": (40.0, 55.0),
    "shotCount": (22, 32),
    "avgShotSec": (1.3, 1.9),
    "shotP10Sec": (0.5, None),
    "shotP90Sec": (None, 3.0),
    "dissolveRatio": (0.40, 0.75),
    "similarityP90": (None, 0.48),
    "maxClipReuse": (1, 2),
    "maxCaptionChars": (1, 14),
    "semanticHitRate": (0.60, None),
}


def _in_range(value: float, bounds: tuple[float | None, float | None]) -> bool:
    lo, hi = bounds
    if lo is not None and value < lo:
        return False
    if hi is not None and value > hi:
        return False
    return True


def evaluate_quality(plan: dict | None, signatures: dict | None, video: Path) -> dict:
    """按 reference/quality-bar.md 打分。没有 plan 时退化为只做规格检查。"""
    if not plan:
        return {"checks": [], "metrics": {}, "ok": True}

    import itertools

    import numpy as np

    shots = plan["shots"]
    durations = [float(s["duration"]) for s in shots]
    lines = plan.get("lines", [])

    dissolves = sum(1 for s in shots if s["transitionOut"]["type"] == "xfade")
    junctions = max(1, len(shots) - 1)
    reuse: dict[str, int] = {}
    for s in shots:
        reuse[s["clipId"]] = reuse.get(s["clipId"], 0) + 1
    adjacent_same = sum(1 for a, b in zip(shots, shots[1:]) if a["clipId"] == b["clipId"])

    hit_rate, demand_count = semantic_hit_rate(plan)
    hit_rate = hit_rate if hit_rate is not None else 1.0

    sim_p90 = None
    if signatures:
        vecs = []
        for s in shots:
            key = f"{s['clipId']}@{s['sourceIn']}"
            vec = signatures.get(key)
            if vec is None:
                # 片段被前后移动过，用最接近的起点兜底
                cands = [k for k in signatures if k.startswith(f"{s['clipId']}@")]
                if cands:
                    vec = signatures[min(cands, key=lambda k: abs(float(k.split("@")[1]) - s["sourceIn"]))]
            if vec is not None:
                vecs.append(np.asarray(vec, dtype=np.float32))
        sims = [float(np.dot(a, b)) for a, b in itertools.combinations(vecs, 2)]
        if sims:
            sim_p90 = round(float(np.percentile(sims, 90)), 3)

    metrics = {
        "durationSec": round(float(plan["totalDuration"]), 3),
        "shotCount": len(shots),
        "avgShotSec": round(float(np.mean(durations)), 3),
        "shotP10Sec": round(float(np.percentile(durations, 10)), 3),
        "shotP90Sec": round(float(np.percentile(durations, 90)), 3),
        "dissolveRatio": round(dissolves / junctions, 3),
        "similarityP90": sim_p90,
        "maxClipReuse": max(reuse.values()) if reuse else 0,
        "adjacentSameClip": adjacent_same,
        "maxCaptionChars": max((len(e["text"]) for e in plan["captions"]["events"]), default=0),
        "semanticHitRate": round(hit_rate, 3),
        "semanticDemandLines": demand_count,
    }

    checks = []
    for key, bounds in QUALITY_BAR.items():
        if key not in metrics or metrics[key] is None:
            continue
        ok = _in_range(float(metrics[key]), bounds)
        checks.append({"metric": key, "value": metrics[key], "target": list(bounds), "ok": ok})
    checks.append({"metric": "adjacentSameClip", "value": adjacent_same, "target": [0, 0],
                   "ok": adjacent_same == 0})
    return {"checks": checks, "metrics": metrics, "ok": all(c["ok"] for c in checks)}



def detect_black(path: Path, min_dur: float = 0.12) -> list[float]:
    t = project.tools()
    proc = subprocess.run(
        [t.ffmpeg, "-hide_banner", "-i", str(path),
         "-vf", f"blackdetect=d={min_dur}:pix_th=0.10", "-an", "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return [round(float(x), 3) for x in re.findall(r"black_start:([0-9.]+)", proc.stderr)]


def detect_silence(path: Path, min_dur: float = 0.35) -> list[float]:
    t = project.tools()
    proc = subprocess.run(
        [t.ffmpeg, "-hide_banner", "-i", str(path),
         "-af", f"silencedetect=n=-45dB:d={min_dur}", "-vn", "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return [round(float(x), 3) for x in re.findall(r"silence_start: ([0-9.]+)", proc.stderr)]


def make_sheet(path: Path, dst: Path, *, samples: int = 12, columns: int = 4) -> Path:
    t = project.tools()
    info = ffmpeg.summary(path)
    fps = max(0.1, samples / max(info["duration"], 0.1))
    rows = (samples + columns - 1) // columns
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [t.ffmpeg, "-v", "error", "-y", "-i", str(path),
         "-vf", f"fps={fps:.4f},scale=240:-2,tile={columns}x{rows}",
         "-frames:v", "1", str(dst)],
        check=True,
    )
    return dst


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=None, help="成片路径；缺省取 output/ 下最新的 mp4")
    ap.add_argument("--plan", default=str(project.WORK / "edit-plan.json"))
    ap.add_argument("--out", default=str(project.WORK / "qc"))
    args = ap.parse_args()

    if args.input:
        video = Path(args.input)
    else:
        vids = sorted(project.OUTPUT.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        if not vids:
            raise SystemExit(f"output 目录里没有成片：{project.OUTPUT}")
        video = vids[-1]

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    with Step("qc") as s:
        info = ffmpeg.summary(video)
        loud = ffmpeg.loudness(video)
        blacks = detect_black(video)
        silences = detect_silence(video)
        plan = read_json(Path(args.plan)) if Path(args.plan).exists() else None
        sheet = make_sheet(video, out_dir / "contact-sheet.jpg")

        issues: list[dict] = []
        lufs = loud.get("input_i", 0.0)
        if not (LUFS_RANGE[0] <= lufs <= LUFS_RANGE[1]):
            issues.append({"level": "block", "code": "LUFS_OUT_OF_RANGE",
                           "detail": f"整体响度 {lufs} LUFS，期望 {LUFS_RANGE}",
                           "fix": "检查 edit-plan.audio.targetLufs 后重跑 render.py",
                           "rerun": "render"})
        if loud.get("input_tp", -99) > -0.5:
            issues.append({"level": "block", "code": "TRUE_PEAK_TOO_HIGH",
                           "detail": f"真峰值 {loud.get('input_tp')} dBTP，可能削波",
                           "fix": "把 edit-plan.audio.truePeakDb 调到 -1.5 后重跑 render.py",
                           "rerun": "render"})
        if blacks:
            issues.append({"level": "warn", "code": "BLACK_FRAMES",
                           "detail": f"检测到黑帧起点：{blacks[:8]}",
                           "fix": "这些时间点对应的镜头入出点越界，检查 edit-plan 里同区间的 sourceIn/sourceOut",
                           "rerun": "plan+render"})
        if silences:
            issues.append({"level": "warn", "code": "SILENCE",
                           "detail": f"检测到静音起点：{silences[:8]}",
                           "fix": "未挂 BGM 时属正常（句间留白）；若已挂 BGM 仍静音，检查 bgm.file 与 gainDb",
                           "rerun": "render"})

        alignment = None
        if plan:
            caption_count = len(plan["captions"]["events"])
            voice_count = len(plan["audio"]["lines"])
            drift = round(plan["totalDuration"] - info["duration"], 3)
            alignment = {
                "captionCount": caption_count,
                "voiceCount": voice_count,
                "expectedDuration": plan["totalDuration"],
                "actualDuration": round(info["duration"], 3),
                "durationDriftSec": drift,
            }
            if caption_count != voice_count:
                issues.append({"level": "block", "code": "CAPTION_VOICE_MISMATCH",
                               "detail": f"字幕 {caption_count} 条 / 配音 {voice_count} 条",
                               "fix": "script.json 的 lines 数量必须与 tts manifest 一致",
                               "rerun": "tts+plan"})
            if abs(drift) > 0.4:
                direction = "短" if drift > 0 else "长"
                issues.append({"level": "block", "code": "DURATION_DRIFT",
                               "detail": f"成片比计划{direction} {abs(drift)}s",
                               "fix": "转场链越界或帧对齐漂移，重跑 plan_edit.py 再渲染",
                               "rerun": "plan+render"})

        sig_path = project.WORK / "material" / "signatures.json"
        signatures = json.loads(sig_path.read_text(encoding="utf-8")) if sig_path.exists() else None
        quality = evaluate_quality(plan, signatures, video)
        for check in quality["checks"]:
            if check["ok"]:
                continue
            metric = check["metric"]
            level = "warn" if metric in (
                "durationSec", "shotCount", "avgShotSec", "shotP10Sec", "shotP90Sec",
                "dissolveRatio", "similarityP90", "maxCaptionChars", "semanticHitRate",
                "maxClipReuse",
            ) else "block"
            issues.append({
                "level": level,
                "code": f"QUALITY_{metric.upper()}",
                "detail": f"{metric}={check['value']}，目标 {check['target']}",
                "fix": "对照 reference/quality-bar.md 定位原因；先查文案是否超出素材，再查语义规则，最后才调参数",
                "rerun": "plan+render",
            })

        report = {
            "schemaVersion": 1,
            "input": str(video),
            "spec": info,
            "loudness": loud,
            "blackFrames": blacks,
            "silences": silences,
            "alignment": alignment,
            "quality": quality,
            "contactSheet": str(sheet),
            "issues": issues,
            "verdict": "fail" if any(i["level"] == "block" for i in issues) else
                       ("warn" if issues or not quality["ok"] else "pass"),
        }
        write_json(out_dir / "qc-report.json", report)

        log(s.name, f"{info['width']}x{info['height']} {info['duration']:.2f}s "
                    f"{info['sizeBytes']/1e6:.1f}MB {info['videoCodec']}")
        log(s.name, f"响度 {lufs} LUFS / 真峰值 {loud.get('input_tp')} dBTP")
        for issue in issues:
            log(s.name, f"{issue['code']}: {issue['detail']}",
                level="error" if issue["level"] == "block" else "warn")
        q = quality["metrics"]
        if q:
            log(s.name, f"结构：{q['shotCount']} 镜 / 均 {q['avgShotSec']}s / "
                        f"P10 {q['shotP10Sec']}s / P90 {q['shotP90Sec']}s / "
                        f"溶解 {q['dissolveRatio']:.0%}")
            log(s.name, f"语义：命中率 {q['semanticHitRate']:.0%}"
                        f"（{q['semanticDemandLines']} 句有画面诉求）/ "
                        f"相似度P90 {q['similarityP90']}")
        failed = [c for c in quality["checks"] if not c["ok"]]
        log(s.name, f"质量基线：{'全部达标' if not failed else f'{len(failed)} 项未达标'}")
        log(s.name, f"结论 {report['verdict']}；缩略图 {sheet}")

    sys.exit(1 if report["verdict"] == "fail" else 0)


if __name__ == "__main__":
    main()
