"""FFmpeg / FFprobe 封装。

只暴露流水线需要的能力，所有调用都带明确错误信息，禁止静默失败。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import project
from .run import log, run, run_json


def _t():
    return project.tools()


def probe(path: Path | str) -> dict:
    t = _t()
    return run_json(
        [
            t.ffprobe, "-v", "error",
            "-show_format", "-show_streams",
            "-of", "json", str(path),
        ],
        step="probe",
    )


def summary(path: Path | str) -> dict:
    """把 probe 结果压成流水线关心的字段。

    注意：iPhone 等设备会把竖拍视频"存成横尺寸 + 旋转元数据"，
    必须按旋转后的显示尺寸计算画幅，否则裁切参数会全错。
    """
    info = probe(path)
    fmt = info.get("format", {})
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v:
        raise RuntimeError(f"文件没有视频流：{path}")
    num, _, den = (v.get("r_frame_rate") or "30/1").partition("/")
    fps = float(num) / float(den or 1)

    rotation = 0
    for side in v.get("side_data_list") or []:
        if side.get("rotation") is not None:
            rotation = int(side["rotation"])
    coded_w, coded_h = int(v.get("width", 0)), int(v.get("height", 0))
    if abs(rotation) % 180 == 90:
        width, height = coded_h, coded_w
    else:
        width, height = coded_w, coded_h

    return {
        "path": str(path),
        "duration": float(fmt.get("duration", 0)),
        "sizeBytes": int(fmt.get("size", 0)),
        "width": width,
        "height": height,
        "codedWidth": coded_w,
        "codedHeight": coded_h,
        "rotation": rotation,
        "fps": round(fps, 3),
        "videoCodec": v.get("codec_name"),
        "pixFmt": v.get("pix_fmt"),
        "audioCodec": a.get("codec_name") if a else None,
        "audioSampleRate": int(a.get("sample_rate", 0)) if a else None,
        "audioChannels": int(a.get("channels", 0)) if a else None,
    }


def scene_cuts_stderr(path: Path | str, threshold: float = 0.25) -> list[float]:
    """基于帧间差分的硬切检测，返回切换时间点（秒）。

    showinfo 输出在 stderr，因此这里直接读 stderr；间隔 <0.2s 的重复点会合并。
    """
    import subprocess

    t = _t()
    proc = subprocess.run(
        [
            t.ffmpeg, "-hide_banner", "-i", str(path),
            "-vf", f"select='gt(scene,{threshold})',showinfo",
            "-an", "-f", "null", "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"[scene] 检测失败：{proc.stderr[-1500:]}")
    times = [float(m) for m in re.findall(r"pts_time:([0-9.]+)", proc.stderr)]
    merged: list[float] = []
    for t0 in times:
        if not merged or t0 - merged[-1] > 0.2:
            merged.append(round(t0, 3))
    return merged


def extract_frames(
    path: Path | str,
    out_dir: Path,
    *,
    fps: float | None = None,
    width: int | None = None,
    prefix: str = "f",
    pattern: str = "jpg",
    quality: int = 3,
) -> list[Path]:
    """按固定帧率抽帧（供逐帧分析）。"""
    t = _t()
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"{prefix}_*.{pattern}"):
        old.unlink()

    vf = []
    if fps:
        vf.append(f"fps={fps}")
    if width:
        vf.append(f"scale={width}:-2:flags=lanczos")
    cmd = [t.ffmpeg, "-v", "error", "-i", str(path)]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += ["-q:v", str(quality), str(out_dir / f"{prefix}_%05d.{pattern}")]
    run(cmd, step="frames")
    return sorted(out_dir.glob(f"{prefix}_*.{pattern}"))


def frame_at(path: Path | str, t: float, out: Path, *, width: int | None = None) -> Path:
    """抽取指定时间点的一帧（精确 seek）。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    vf = f"scale={width}:-2:flags=lanczos" if width else "null"
    run(
        [
            _t().ffmpeg, "-v", "error",
            "-ss", f"{max(t, 0):.3f}", "-i", str(path),
            "-frames:v", "1", "-vf", vf, "-y", str(out),
        ],
        step="frame",
    )
    if not out.exists():
        raise RuntimeError(f"[frame] 抽帧失败：{path} @ {t}s")
    return out


def extract_audio(path: Path | str, out: Path, *, sr: int = 22050, mono: bool = True) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_t().ffmpeg, "-v", "error", "-i", str(path), "-vn", "-ar", str(sr)]
    if mono:
        cmd += ["-ac", "1"]
    cmd += ["-y", str(out)]
    run(cmd, step="audio")
    return out


def loudness(path: Path | str) -> dict:
    """测量整体响度（EBU R128）。"""
    import subprocess

    t = _t()
    proc = subprocess.run(
        [
            t.ffmpeg, "-hide_banner", "-i", str(path),
            "-af", "loudnorm=print_format=json", "-f", "null", "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr, re.S)
    if not m:
        raise RuntimeError(f"[loudness] 无法解析响度：{proc.stderr[-800:]}")
    data = json.loads(m.group(0))
    out = {}
    for k, v in data.items():
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            out[k] = v
    return out


def capabilities() -> dict:
    """探测 ffmpeg 是否具备流水线必需的能力。"""
    import subprocess

    t = _t()
    filters = subprocess.run(
        [t.ffmpeg, "-hide_banner", "-filters"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    ).stdout
    encoders = subprocess.run(
        [t.ffmpeg, "-hide_banner", "-encoders"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    ).stdout
    required_filters = ["xfade", "loudnorm", "sidechaincompress", "subtitles", "ass", "overlay"]
    required_encoders = ["libx264", "aac"]
    return {
        "ffmpeg": t.ffmpeg,
        "ffprobe": t.ffprobe,
        "filters": {f: f" {f} " in filters for f in required_filters},
        "encoders": {e: f" {e} " in encoders for e in required_encoders},
        "ok": all(f" {f} " in filters for f in required_filters)
              and all(f" {e} " in encoders for e in required_encoders),
    }
