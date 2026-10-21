#!/usr/bin/env python3
"""make_label_sheets.py — 把素材缩略图拼成标注图，交给 AI 自己看图打标

为什么这么做：
  画面识别的质量决定成片水平。与其让脚本去调一个固定的视觉 API，
  不如让 opencode 里正在跑的模型（客户用哪个就是哪个）直接看图。

流程：
  1. 本脚本生成 9 宫格标注图 + 空模板 labels.template.json
  2. AI 读图，把每个素材的「主体｜动作｜细节」填进模板，存成 labels.json
  3. analyze_material.py --labels labels.json 合并进素材索引

产出：work/material/labeling/
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw, ImageFont

from lib import project
from lib.run import Step, log, read_json, write_json

COLS, ROWS = 3, 3
CELL_W, CELL_H = 420, 560
LABEL_H = 34


def _font(size: int) -> ImageFont.FreeTypeFont:
    for cand in ("/System/Library/Fonts/PingFang.ttc",
                 "/System/Library/Fonts/STHeiti Medium.ttc"):
        if Path(cand).exists():
            try:
                return ImageFont.truetype(cand, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _load(path: Path, box: tuple[int, int]) -> Image.Image:
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail(box, Image.LANCZOS)
    canvas = Image.new("RGB", box, (18, 18, 18))
    canvas.paste(im, ((box[0] - im.width) // 2, (box[1] - im.height) // 2))
    return canvas


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--material", default=str(project.WORK / "material" / "material-index.json"))
    ap.add_argument("--out", default=str(project.WORK / "material" / "labeling"))
    args = ap.parse_args()

    index = read_json(Path(args.material))
    clips = [c for c in index["clips"] if c.get("representativeFrame")]
    if not clips:
        raise SystemExit("素材索引里没有代表帧，先跑 analyze_material.py")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    font = _font(24)
    per_sheet = COLS * ROWS
    sheets = []

    with Step("label-sheets") as s:
        for page in range(0, len(clips), per_sheet):
            batch = clips[page: page + per_sheet]
            sheet = Image.new("RGB", (COLS * CELL_W, ROWS * (CELL_H + LABEL_H)), (12, 12, 12))
            draw = ImageDraw.Draw(sheet)
            for i, clip in enumerate(batch):
                r, c = divmod(i, COLS)
                x, y = c * CELL_W, r * (CELL_H + LABEL_H)
                img = _load(project.ROOT / clip["representativeFrame"], (CELL_W, CELL_H))
                sheet.paste(img, (x, y))
                draw.rectangle([x, y + CELL_H, x + CELL_W, y + CELL_H + LABEL_H], fill=(0, 0, 0))
                draw.text((x + 10, y + CELL_H + 5), clip["id"], font=font, fill=(255, 220, 80))
            dst = out / f"sheet_{page // per_sheet + 1:02d}.jpg"
            sheet.save(dst, quality=90)
            sheets.append(dst.name)
            log(s.name, f"{dst.name}：{len(batch)} 个素材（{batch[0]['id']} … {batch[-1]['id']}）")

    template = {
        "_说明": (
            "把每个素材的「主体｜动作｜细节」填进来。分不清就留空，不要编。\n"
            "主体：颜色+款式，例如 棕色三角杯内衣 / 豹纹内衣 / 裸色杯垫 / 透明包装袋\n"
            "动作：手在做什么，例如 平铺陈列 / 手托正面 / 翻到背面 / 扣合前扣 / 拉扯肩带 / 多装入袋 / 多款并排对比\n"
            "细节：从 前扣、肩带、杯垫、后背、侧边、底围、浅蓝边、豹纹、包装袋、多款 里选最多三个，逗号分隔"
        )
    }
    for clip in clips:
        template[clip["id"]] = {"subject": "", "action": "", "details": ""}
    write_json(out / "labels.template.json", template)

    (out / "README.md").write_text(
        "\n".join([
            "# 素材标注任务",
            "",
            f"共 {len(clips)} 个素材，分 {len(sheets)} 张九宫格，每格左上角是素材编号。",
            "",
            "## 你要做的",
            "",
            "1. 逐张打开 `sheet_XX.jpg`，看清每一格里**商品颜色款式**、**手在做什么**、**能看到的局部细节**。",
            "2. 把结果填进 `labels.template.json`，每个素材三行：",
            "",
            "```json",
            f'"{clips[0]["id"]}": {{',
            '  "subject": "棕色三角杯内衣",',
            '  "action": "手托正面",',
            '  "details": "前扣,浅蓝边"',
            "}",
            "```",
            "",
            "3. 存成 `labels.json`，然后运行：",
            "",
            "```bash",
            f".tools/py/pipeline/bin/python .opencode/skills/video-producer/scripts/analyze_material.py --labels {out / 'labels.json'}",
            "```",
            "",
            "## 注意",
            "",
            "- **只写画面上真的能看见的**。看不清就留空，不要猜。",
            "- 细节词只能用：前扣、肩带、杯垫、后背、侧边、底围、浅蓝边、豹纹、包装袋、多款。",
            "- 这些标注会直接决定成片里画面和口播能不能对上，写错等于成片出错。",
        ]) + "\n",
        encoding="utf-8",
    )

    log("label-sheets", f"{len(clips)} 个素材 / {len(sheets)} 张图 → {out}")
    log("label-sheets", "下一步：读图 → 填 labels.template.json → 存为 labels.json")


if __name__ == "__main__":
    main()
