#!/usr/bin/env python3
"""render.py — 按 edit-plan.json 渲染成片

执行顺序：
  1. 逐镜头归一化（裁切 → 缩放 → 帧率 → 调色）→ 中间片段
  2. 硬切拼接 → 无音轨视频
  3. 配音按时间轴落位 + BGM 闪避 + 响度标准化 → 音轨
  4. 烧录 ASS 字幕 + 混流封装 → 成片

所有中间产物留在 work/render 下，任一阶段可单独重跑。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import ffmpeg, project
from lib.run import Step, log, read_json, run, write_json


# ---------------------------------------------------------------- 字幕


def ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def ass_escape(text: str) -> str:
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", " ")


def write_ass(plan: dict, path: Path) -> Path:
    st = plan["captions"]["style"]
    w, h = plan["canvas"]["width"], plan["canvas"]["height"]
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {w}",
        f"PlayResY: {h}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: BT.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Caption,{st['fontFamily']},{st['fontSize']},{st['primaryColor']},"
        f"&H00FFFFFF,{st['outlineColor']},&H80000000,1,0,0,0,100,100,0,0,1,"
        f"{st['outlineWidth']},{st['shadow']},2,60,60,{st['marginV']},1",
        f"Style: Notice,{st['fontFamily']},{max(12, round(h * 0.0155))},&H00FFFFFF,"
        f"&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,9,0,{round(w * 0.03)},"
        f"{round(h * 0.022)},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    disc = plan["captions"].get("disclaimer")
    if disc:
        lines.append(
            f"Dialogue: 0,{ass_time(0)},{ass_time(plan['totalDuration'])},Notice,,0,0,0,,"
            f"{ass_escape(disc['text'])}"
        )

    for ev in plan["captions"]["events"]:
        lines.append(
            f"Dialogue: 0,{ass_time(ev['start'])},{ass_time(ev['end'])},Caption,,0,0,0,,"
            f"{ass_escape(ev['text'])}"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- 视频


def shot_filter(shot: dict, canvas: dict) -> str:
    c = shot["crop"]
    g = shot.get("grade") or {}
    eq = g.get("eq") or {}
    cb = g.get("colorbalance") or {}
    parts = [
        f"crop={c['w']}:{c['h']}:{c['x']}:{c['y']}",
        f"scale={canvas['width']}:{canvas['height']}:flags=lanczos",
        f"fps={canvas['fps']}",
        "setsar=1",
    ]
    if eq:
        parts.append(
            "eq=brightness={brightness}:contrast={contrast}:saturation={saturation}:gamma={gamma}".format(
                brightness=eq.get("brightness", 0.0),
                contrast=eq.get("contrast", 1.0),
                saturation=eq.get("saturation", 1.0),
                gamma=eq.get("gamma", 1.0),
            )
        )
    if cb:
        parts.append(
            "colorbalance=rs={rs}:gs={gs}:bs={bs}".format(
                rs=cb.get("rs", 0.0), gs=cb.get("gs", 0.0), bs=cb.get("bs", 0.0)
            )
        )
    parts.append("format=yuv420p")
    return ",".join(parts)


def render_shots(plan: dict, out_dir: Path, *, preset: str, crf: int) -> list[Path]:
    t = project.tools()
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    fps = plan["canvas"]["fps"]
    for i, shot in enumerate(plan["shots"], 1):
        dst = out_dir / f"shot_{i:03d}.mp4"
        render_frames = int(shot.get("renderFrames") or round(shot["duration"] * fps))
        pad = 0.6  # 源素材不足时用冻结尾帧补齐，保证帧数精确
        vf = shot_filter(shot, plan["canvas"])

        if shot.get("static"):
            # 静态图：循环成视频流，不需要 seek 也不需要补帧
            cmd = [
                t.ffmpeg, "-v", "error", "-y",
                "-loop", "1", "-framerate", str(fps),
                "-i", str(project.ROOT / shot["source"]),
                "-vf", vf,
                "-frames:v", str(render_frames),
                "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                str(dst),
            ]
        else:
            render_sec = render_frames / fps + pad
            cmd = [
                t.ffmpeg, "-v", "error", "-y",
                "-ss", f"{shot['sourceIn']:.3f}",
                "-i", str(project.ROOT / shot["source"]),
                "-t", f"{render_sec:.3f}",
                "-an",
                "-vf", vf + f",tpad=stop_mode=clone:stop_duration={pad}",
                "-frames:v", str(render_frames),
                "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                str(dst),
            ]
        run(cmd, step=f"shot/{i:02d}")
        if not dst.exists() or dst.stat().st_size == 0:
            raise RuntimeError(f"镜头 {i} 渲染失败：{dst}")
        paths.append(dst)
    return paths


def compose_video(plan: dict, clips: list[Path], dst: Path, work: Path,
                  *, preset: str, crf: int) -> Path:
    """按计划拼接镜头，需要过渡的地方用 xfade，其余用单帧硬切。

    这里必须用 filter_complex 串 xfade 链而不是 concat 解复用器：
    concat 只能硬切，而我们要在同一遍编码里混用两种衔接方式。
    """
    t = project.tools()
    if len(clips) == 1:
        run([t.ffmpeg, "-v", "error", "-y", "-i", str(clips[0]),
             "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
             "-pix_fmt", "yuv420p", str(dst)], step="compose")
        return dst

    inputs: list[str] = []
    for p in clips:
        inputs += ["-i", str(p)]

    chains = [
        f"[{i}:v]format=yuv420p,setsar=1,settb=AVTB,setpts=PTS-STARTPTS[v{i}]"
        for i in range(len(clips))
    ]
    prev = "v0"
    for i in range(1, len(clips)):
        junction = plan["shots"][i - 1]["transitionOut"]
        duration = float(junction["duration"])
        # xfade 要求 offset+duration 严格小于前一段长度，否则会截断整条链。
        # 往前挪 1ms 即可满足；32 个衔接累计漂移约 32ms，听感上不可察觉。
        offset = max(0.0, float(plan["shots"][i]["start"]) - 0.001)
        if junction["type"] == "cut":
            duration = max(1.0 / plan["canvas"]["fps"], duration)
        out = f"x{i}"
        chains.append(
            f"[{prev}][v{i}]xfade=transition=fade:duration={duration:.3f}:"
            f"offset={offset:.3f}[{out}]"
        )
        prev = out

    graph = ";".join(chains)
    (work / "compose-filter.txt").write_text(graph, encoding="utf-8")
    run(
        [t.ffmpeg, "-v", "error", "-y", *inputs,
         "-filter_complex", graph, "-map", f"[{prev}]",
         "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dst)],
        step="compose",
    )
    return dst


# ---------------------------------------------------------------- 音频


def build_audio(plan: dict, dst: Path, work: Path) -> Path:
    t = project.tools()
    total = float(plan["totalDuration"])
    lines = plan["audio"]["lines"]
    bgm = plan["audio"].get("bgm") or {}
    bgm_file = bgm.get("file")

    inputs: list[str] = []
    for line in lines:
        inputs += ["-i", str(project.ROOT / line["file"])]
    bgm_index = None
    if bgm_file:
        bgm_index = len(lines)
        inputs += ["-stream_loop", "-1", "-t", f"{total:.3f}",
                   "-i", str(project.ROOT / bgm_file)]

    chains = []
    voice_labels = []
    for i, line in enumerate(lines):
        delay = int(round(float(line["start"]) * 1000))
        label = f"v{i}"
        chains.append(
            f"[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"adelay={delay}|{delay},volume=1.0[{label}]"
        )
        voice_labels.append(f"[{label}]")
    chains.append(
        "".join(voice_labels) + f"amix=inputs={len(voice_labels)}:normalize=0:dropout_transition=0[voice]"
    )

    if bgm_index is not None:
        gain = float(bgm.get("gainDb", -20.0))
        fade = float(bgm.get("fadeOutSec", 1.2))
        chains.append(
            f"[{bgm_index}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume={gain}dB,atrim=0:{total:.3f},asetpts=N/SR/TB,"
            f"afade=t=in:st=0:d=0.4,afade=t=out:st={max(0.0, total - fade):.3f}:d={fade}[bg]"
        )
        # BGM 被配音压下去，保证口播清晰
        chains.append(
            "[bg][voice]sidechaincompress=threshold=0.03:ratio=8:attack=15:release=350:makeup=1[bgd]"
        )
        chains.append(
            f"[bgd][voice]amix=inputs=2:normalize=0:dropout_transition=0,"
            f"loudnorm=I={plan['audio']['targetLufs']}:TP={plan['audio']['truePeakDb']}:LRA=11,"
            f"alimiter=limit=0.97[aout]"
        )
    else:
        chains.append(
            f"[voice]loudnorm=I={plan['audio']['targetLufs']}:TP={plan['audio']['truePeakDb']}:LRA=11,"
            f"alimiter=limit=0.97[aout]"
        )

    graph = ";".join(chains)
    (work / "audio-filter.txt").write_text(graph, encoding="utf-8")
    run(
        [t.ffmpeg, "-v", "error", "-y", *inputs,
         "-filter_complex", graph,
         "-map", "[aout]", "-t", f"{total:.3f}",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         str(dst)],
        step="audio",
    )
    return dst


# ---------------------------------------------------------------- 主流程


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default=str(project.WORK / "edit-plan.json"))
    ap.add_argument("--out", default=None, help="输出文件路径")
    ap.add_argument("--name", default="预览", help="输出文件名（不含扩展名），默认写到 成片/预览.mp4")
    ap.add_argument("--preset", default="medium")
    ap.add_argument("--crf", type=int, default=18)
    args = ap.parse_args()

    plan = read_json(Path(args.plan))
    work = project.WORK / "render"
    work.mkdir(parents=True, exist_ok=True)

    out = Path(args.out) if args.out else project.OUTPUT / f"{args.name or '预览'}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)

    with Step("render/video") as s:
        clips = render_shots(plan, work / "shots", preset="veryfast", crf=16)
        video = compose_video(plan, clips, work / "video.mp4", work,
                              preset="veryfast", crf=16)
        log(s.name, f"{len(clips)} 个镜头 → {video.name}")

    with Step("render/audio") as s:
        audio = build_audio(plan, work / "audio.m4a", work)
        log(s.name, f"音轨 {audio.name}")

    with Step("render/subtitles") as s:
        ass = write_ass(plan, work / "captions.ass")
        log(s.name, f"{len(plan['captions']['events'])} 条字幕")

    with Step("render/final") as s:
        t = project.tools()
        run(
            [t.ffmpeg, "-v", "error", "-y",
             "-i", str(video), "-i", str(audio),
             "-vf", f"ass={ass}",
             "-map", "0:v:0", "-map", "1:a:0",
             "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
             "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
             "-movflags", "+faststart", "-shortest",
             "-metadata", "comment=generated by video-producer pipeline",
             str(out)],
            step="final",
        )
        info = ffmpeg.summary(out)
        log(s.name, f"{info['width']}x{info['height']} {info['duration']:.2f}s "
                    f"{(out.stat().st_size/1e6):.1f}MB → {out}")

    write_json(work / "render-manifest.json", {
        "plan": str(Path(args.plan).relative_to(project.ROOT)),
        "output": str(out.relative_to(project.ROOT)) if out.is_relative_to(project.ROOT) else str(out),
        "shots": len(plan["shots"]),
        "totalDuration": plan["totalDuration"],
    })


if __name__ == "__main__":
    main()
