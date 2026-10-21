#!/usr/bin/env python3
"""review.py — 生成"可被审阅"的证据包

目的：让 AI 和用户不靠"我觉得还行"来判断成片，而是看具体证据：
  1. 逐镜头缩略图（带编号/句子号/时长），一眼看出镜头选择是否合理
  2. 每个转场的三帧对照（转场前 / 转场中 / 转场后），确认过渡是否自然
  3. 镜头表（markdown），标出每个镜头的入出点、尾部运动量、转场类型
  4. summary.json，供程序读取

产出：work/review/

用法：
  python scripts/review.py --input output/final.mp4
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw, ImageFont

from lib import ffmpeg, project
from lib.run import Step, log, read_json, write_json

GRID_COLS = 6
THUMB_W = 240
LABEL_H = 26


def _font(size: int) -> ImageFont.FreeTypeFont:
    for cand in ("/System/Library/Fonts/PingFang.ttc",
                 "/System/Library/Fonts/STHeiti Medium.ttc",
                 "/System/Library/Fonts/Hiragino Sans GB.ttc"):
        if Path(cand).exists():
            try:
                return ImageFont.truetype(cand, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _label(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font) -> None:
    x, y = xy
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0))
    draw.text((x, y), text, font=font, fill=(255, 255, 255))


def build_contact_sheet(items: list[dict], dst: Path) -> Path:
    if not items:
        raise RuntimeError("没有可用的镜头缩略图")
    with Image.open(items[0]["frame"]) as im0:
        tw, th = im0.size
    rows = (len(items) + GRID_COLS - 1) // GRID_COLS
    sheet = Image.new("RGB", (GRID_COLS * tw, rows * (th + LABEL_H)), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    font = _font(18)
    for idx, item in enumerate(items):
        r, c = divmod(idx, GRID_COLS)
        x, y = c * tw, r * (th + LABEL_H)
        with Image.open(item["frame"]) as im:
            sheet.paste(im.convert("RGB").resize((tw, th), Image.LANCZOS), (x, y))
        _label(draw, (x + 6, y + th + 3),
               f"#{item['index']:02d} L{item['lineIndex']:02d} {item['duration']:.1f}s {item['transition']}", font)
    dst.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dst, quality=90)
    return dst


def build_transition_strip(plan: dict, video: Path, work: Path, dst: Path) -> Path | None:
    """把每个溶解转场的前/中/后三帧并排，便于判断过渡是否生硬。"""
    junctions = [i for i, s in enumerate(plan["shots"][:-1])
                 if s["transitionOut"]["type"] == "xfade"]
    if not junctions:
        return None

    font = _font(16)
    tiles: list[Image.Image] = []
    for i in junctions:
        shot = plan["shots"][i]
        start = float(shot["start"]) + float(shot["duration"]) - 0.001
        d = float(shot["transitionOut"]["duration"])
        for k, t in enumerate((start - 0.10, start + d / 2, start + d + 0.10)):
            f = ffmpeg.frame_at(video, max(0.0, t), work / "frames" / f"tr_{i:02d}_{k}.jpg",
                                width=THUMB_W)
            with Image.open(f) as im:
                tiles.append(im.convert("RGB").resize((THUMB_W, int(THUMB_W * 16 / 9)), Image.LANCZOS))
        tiles[-3] = _annotate(tiles[-3], f"#{i + 1} 转场前", font)
        tiles[-2] = _annotate(tiles[-2], f"溶解 {d:.2f}s", font)
        tiles[-1] = _annotate(tiles[-1], f"#{i + 2} 进入", font)

    if not tiles:
        return None
    tw, th = tiles[0].size
    rows = (len(tiles) + 2) // 3
    sheet = Image.new("RGB", (3 * tw, rows * th), (20, 20, 20))
    for idx, tile in enumerate(tiles):
        r, c = divmod(idx, 3)
        sheet.paste(tile, (c * tw, r * th))
    dst.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dst, quality=90)
    return dst


def _annotate(img: Image.Image, text: str, font) -> Image.Image:
    bar = Image.new("RGB", (img.width, 22), (0, 0, 0))
    bar.paste(img.crop((0, 0, img.width, 22)), (0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, img.width, 22], fill=(0, 0, 0))
    draw.text((6, 3), text, font=font, fill=(255, 220, 80))
    return img


def shot_table(plan: dict) -> str:
    total = plan["totalDuration"]
    lines = [
        "# 镜头表",
        "",
        f"- 画布：{plan['canvas']['width']}×{plan['canvas']['height']} @ {plan['canvas']['fps']}fps"
        f"（{plan['canvas'].get('mode', 'source')}）",
        f"- 总长：{total:.2f}s，镜头 {len(plan['shots'])} 个，句子 {len(plan.get('lines', []))} 句",
        "",
        "## 口播与画面对齐",
        "",
        "| 句 | 口播 | 镜头主体 | 画面动作 | 语义命中 |",
        "|---|---|---|---|---|",
    ]
    seen: set[int] = set()
    for s in plan["shots"]:
        line_text = s.get("caption", "")
        if s["lineIndex"] in seen:
            line_text = f"↳ {line_text}"
        seen.add(s["lineIndex"])
        lines.append(
            f"| {s['lineIndex']} | {line_text} | {s.get('subject') or '-'} | "
            f"{s.get('action') or '-'} | {s.get('semanticScore', '-')} |"
        )

    lines += [
        "",
        "## 镜头明细",
        "",
        "| # | 句 | 时间 | 时长 | 素材 | 入点 | 出点 | 尾部运动 | 转场 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in plan["shots"]:
        t = s.get("tailMotion")
        motion = f"{t:.3f}" if isinstance(t, (int, float)) else "-"
        lines.append(
            f"| {s['index']} | {s['lineIndex']} | {s['start']:.2f}–{s['end']:.2f} | "
            f"{s['duration']:.2f}s | {s['clipId']} | {s['sourceIn']:.2f} | {s['sourceOut']:.2f} | "
            f"{motion} | {s['transitionOut']['type']}"
            f"{' ' + format(s['transitionOut']['duration'], '.2f') + 's' if s['transitionOut']['type'] == 'xfade' else ''} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=None, help="成片路径；缺省取 output/ 下最新 mp4")
    ap.add_argument("--plan", default=str(project.WORK / "edit-plan.json"))
    ap.add_argument("--out", default=str(project.WORK / "review"))
    args = ap.parse_args()

    plan = read_json(Path(args.plan))
    video = Path(args.input) if args.input else sorted(
        project.OUTPUT.glob("*.mp4"), key=lambda p: p.stat().st_mtime)[-1]
    out = Path(args.out)
    work = out / "_frames"
    work.mkdir(parents=True, exist_ok=True)

    with Step("review") as s:
        items = []
        for shot in plan["shots"]:
            t = shot["start"] + shot["duration"] / 2
            frame = ffmpeg.frame_at(video, t, work / f"shot_{shot['index']:03d}.jpg", width=THUMB_W)
            items.append({
                "index": shot["index"], "lineIndex": shot["lineIndex"],
                "duration": shot["duration"], "frame": str(frame),
                "transition": shot["transitionOut"]["type"],
            })
        sheet = build_contact_sheet(items, out / "contact-sheet.jpg")
        strips = build_transition_strip(plan, video, work, out / "transitions.jpg")
        table = shot_table(plan)
        (out / "shot-table.md").write_text(table, encoding="utf-8")

        write_json(out / "summary.json", {
            "video": str(video),
            "shots": len(plan["shots"]),
            "lines": len(plan.get("lines", [])),
            "totalDuration": plan["totalDuration"],
            "transitions": sum(1 for s in plan["shots"] if s["transitionOut"]["type"] == "xfade"),
            "contactSheet": str(sheet),
            "transitionStrip": str(strips) if strips else None,
            "shotTable": str(out / "shot-table.md"),
        })
        log(s.name, f"缩略图 {sheet}")
        if strips:
            log(s.name, f"转场对照 {strips}")
        log(s.name, f"镜头表 {out / 'shot-table.md'}")


if __name__ == "__main__":
    main()
