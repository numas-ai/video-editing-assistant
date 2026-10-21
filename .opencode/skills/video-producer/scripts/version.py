#!/usr/bin/env python3
"""version.py — 成片版本库

解决一个真实问题：客户会说"第一版好"，过一会儿又说"第三版好"，最后又回到第一版。
所以每次渲染都必须留档，永不覆盖，且能一键回到任意一版继续改。

设计约束：
  - 版本目录一旦写入就不再修改（只读）
  - 每次保存同时快照 script.json / edit-plan.json / tts 清单，
    否则客户回到 v1 时，文案和时间线已经变了，回不去
  - output/ 下只有「预览.mp4」会被覆盖，其余都是编号版本

用法：
  python scripts/version.py save --note "第一版，偏慢"
  python scripts/version.py list
  python scripts/version.py restore v01
  python scripts/version.py diff v01 v03
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import ffmpeg, project
from lib.quality import semantic_hit_rate
from lib.run import Step, log, read_json, write_json

PREVIEW_NAME = "预览.mp4"
MANIFEST = "版本清单.md"


def version_dirs(out: Path) -> list[Path]:
    return sorted(p for p in out.glob("v[0-9][0-9]") if p.is_dir())


def next_version(out: Path) -> str:
    existing = [int(p.name[1:]) for p in version_dirs(out)]
    return f"v{(max(existing) + 1 if existing else 1):02d}"


def find_preview(out: Path, explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise SystemExit(f"找不到要保存的视频：{p}")
        return p
    preview = out / PREVIEW_NAME
    if preview.exists():
        return preview
    loose = [p for p in out.glob("*.mp4") if p.is_file()]
    if not loose:
        raise SystemExit(f"{out} 里没有可保存的成片，先运行 render.py")
    return max(loose, key=lambda p: p.stat().st_mtime)


def make_cover(video: Path, dst: Path) -> None:
    """九宫格封面，客户在 Finder 里一眼能认出是哪一版。"""
    t = project.tools()
    subprocess.run(
        [t.ffmpeg, "-v", "error", "-y", "-i", str(video),
         "-vf", "fps=1/5,scale=320:-2,tile=3x3", "-frames:v", "1", str(dst)],
        check=True,
    )


def load_meta(vdir: Path) -> dict:
    p = vdir / "meta.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def write_manifest(out: Path) -> Path:
    rows = [
        "# 版本清单",
        "",
        "每次生成的成片都在这里，**不会被覆盖**。想回到某一版，直接打开对应文件夹，",
        "或者跟 AI 说「以 v03 为基础继续改」。",
        "",
        "| 版本 | 时间 | 时长 | 说明 |",
        "|---|---|---|---|",
    ]
    for vdir in version_dirs(out):
        meta = load_meta(vdir)
        rows.append(
            f"| [{vdir.name}](./{vdir.name}/) | {meta.get('createdAt', '-')} | "
            f"{meta.get('durationSec', '-')}s | {meta.get('note', '')} |"
        )
    path = out / MANIFEST
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def cmd_save(args) -> None:
    out = project.OUTPUT
    out.mkdir(parents=True, exist_ok=True)
    video = find_preview(out, args.video)
    name = next_version(out)
    vdir = out / name
    if vdir.exists():
        raise SystemExit(f"{vdir} 已存在，版本目录不允许覆盖")

    with Step(f"version/{name}") as s:
        vdir.mkdir(parents=True)
        shutil.copy2(video, vdir / "成片.mp4")
        info = ffmpeg.summary(vdir / "成片.mp4")
        make_cover(vdir / "成片.mp4", vdir / "封面.jpg")

        # 快照：回到这一版时，文案和时间线必须一起回来
        for src, dst in (
            (project.WORK / "script.json", "script.json"),
            (project.WORK / "edit-plan.json", "edit-plan.json"),
            (project.WORK / "tts" / "manifest.json", "tts-manifest.json"),
            (project.WORK / "reference" / "style-spec.json", "style-spec.json"),
        ):
            if src.exists():
                shutil.copy2(src, vdir / dst)

        plan_path = vdir / "edit-plan.json"
        plan = read_json(plan_path) if plan_path.exists() else {}
        shots = plan.get("shots", [])
        lines = plan.get("lines", [])
        hit_rate, _demand = semantic_hit_rate(plan)
        meta = {
            "version": name,
            "createdAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "note": args.note or "",
            "durationSec": round(info["duration"], 2),
            "width": info["width"],
            "height": info["height"],
            "shots": len(shots),
            "lines": len(lines),
            "semanticHitRate": hit_rate,
            "sizeBytes": info["sizeBytes"],
        }
        write_json(vdir / "meta.json", meta)

        (vdir / "说明.md").write_text(
            "\n".join([
                f"# {name}",
                "",
                f"- 生成时间：{meta['createdAt']}",
                f"- 时长：{meta['durationSec']}s",
                f"- 画幅：{meta['width']}×{meta['height']}",
                f"- 镜头数：{meta['shots']}",
                f"- 语义命中率：{meta['semanticHitRate']}",
                f"- 这一版改了什么：{meta['note'] or '（未填写）'}",
                "",
                "这个文件夹是只读快照，不要改里面的文件。",
                "要基于这一版继续改，跟 AI 说「以 {} 为基础继续改」。".format(name),
            ]) + "\n",
            encoding="utf-8",
        )

    manifest = write_manifest(out)
    log("version", f"{name} 已保存 → {vdir}")
    log("version", f"清单：{manifest}")


def cmd_list(args) -> None:
    dirs = version_dirs(project.OUTPUT)
    if not dirs:
        print("还没有任何版本。先渲染，再运行 version.py save")
        return
    print(f"{'版本':<6}{'时间':<21}{'时长':<8}{'镜头':<6}{'说明'}")
    for vdir in dirs:
        m = load_meta(vdir)
        print(f"{vdir.name:<6}{m.get('createdAt', '-'):<21}"
              f"{str(m.get('durationSec', '-')) + 's':<8}{str(m.get('shots', '-')):<6}{m.get('note', '')}")


def cmd_restore(args) -> None:
    vdir = project.OUTPUT / args.version
    if not vdir.is_dir():
        raise SystemExit(f"没有这个版本：{vdir}")
    for src, dst in (
        ("script.json", project.WORK / "script.json"),
        ("edit-plan.json", project.WORK / "edit-plan.json"),
        ("tts-manifest.json", project.WORK / "tts" / "manifest.json"),
    ):
        s = vdir / src
        if s.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, dst)
            print(f"恢复 {src} → {dst}")

    # 文案变了就必须重新配音，否则音频和字幕对不上
    old_tts = vdir / "tts-manifest.json"
    need_tts = True
    if old_tts.exists():
        manifest = read_json(old_tts)
        files = [project.ROOT / l["file"] for l in manifest.get("lines", [])]
        need_tts = not all(f.exists() for f in files)
        print("配音文件齐全" if not need_tts else "配音文件缺失，需要重跑 tts.py")

    log("restore", f"已回到 {args.version}")
    log("restore", "下一步：" + ("重跑 tts.py → render.py" if need_tts else "直接重跑 render.py"))


def cmd_diff(args) -> None:
    a, b = project.OUTPUT / args.from_version, project.OUTPUT / args.to_version
    for v in (a, b):
        if not v.is_dir():
            raise SystemExit(f"没有这个版本：{v}")
    ma, mb = load_meta(a), load_meta(b)
    print(f"{a.name} → {b.name}\n")
    for key in ("durationSec", "shots", "semanticHitRate", "note"):
        print(f"  {key:<16}{ma.get(key, '-')}  →  {mb.get(key, '-')}")

    pa, pb = a / "script.json", b / "script.json"
    if pa.exists() and pb.exists():
        la = [l["text"] for l in read_json(pa).get("lines", [])]
        lb = [l["text"] for l in read_json(pb).get("lines", [])]
        changed = [(i + 1, x, y) for i, (x, y) in enumerate(zip(la, lb)) if x != y]
        print(f"\n  文案改动：{len(changed)} 句")
        for i, x, y in changed[:12]:
            print(f"    #{i}  {x}  →  {y}")


def main() -> None:
    ap = argparse.ArgumentParser(description="成片版本库")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("save", help="把当前预览保存为新版本")
    s.add_argument("--note", default="", help="这一版改了什么，客户能看懂的一句话")
    s.add_argument("--video", default=None, help="默认保存 成片/预览.mp4")
    s.set_defaults(func=cmd_save)

    l = sub.add_parser("list", help="列出版本")
    l.set_defaults(func=cmd_list)

    r = sub.add_parser("restore", help="回到某一版继续改")
    r.add_argument("version")
    r.set_defaults(func=cmd_restore)

    d = sub.add_parser("diff", help="对比两版差异")
    d.add_argument("from_version")
    d.add_argument("to_version")
    d.set_defaults(func=cmd_diff)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
