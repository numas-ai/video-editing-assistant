#!/usr/bin/env python3
"""doctor.py — 环境自检

在干净机器上部署后先跑这个：逐项确认依赖是否就位，并给出可执行的修复命令。
阻断级缺失会让脚本以非 0 退出，方便 CI/交付验收。

用法：python scripts/doctor.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import ffmpeg, project, vlm

# 提示里的命令必须能从项目根直接复制执行
S = project.SKILL_DIR.relative_to(project.ROOT) / "scripts"

OK, WARN, FAIL = "✓", "!", "✗"


def line(mark: str, name: str, detail: str = "", fix: str = "") -> dict:
    print(f" {mark}  {name}" + (f"  —  {detail}" if detail else ""))
    if fix and mark != OK:
        print(f"      → {fix}")
    return {"mark": mark, "name": name, "detail": detail, "fix": fix}


def check_ffmpeg() -> dict:
    caps = ffmpeg.capabilities()
    missing = [k for k, v in caps["filters"].items() if not v] + \
              [k for k, v in caps["encoders"].items() if not v]
    if caps["ok"]:
        return line(OK, "ffmpeg 能力", f"{caps['ffmpeg']}")
    return line(FAIL, "ffmpeg 能力", f"缺少 {', '.join(missing)}",
                "brew install ffmpeg-full  # 需要 libass/drawtext/xfade")


def check_python() -> dict:
    try:
        import numpy, PIL, librosa  # noqa: F401
        return line(OK, "流水线 Python 依赖", f"numpy/Pillow/librosa 可用（{sys.executable}）")
    except ImportError as exc:
        return line(FAIL, "流水线 Python 依赖", str(exc), f"bash {S}/setup.sh --python-only")


def check_vlm() -> dict:
    health = vlm.health()
    label = "视觉模型（生产级）" if health["provider"] != "ollama" else "视觉模型（本地）"
    detail = f"{health['model']} @ {health['provider']}"
    if health["ok"]:
        return line(OK, label, detail)
    if health["provider"] == "ollama":
        fix = f"ollama serve && ollama pull {health['model']}（或改用生产级云端模型）"
    else:
        fix = (f"检查 {vlm.KEY_ENV_NAME} 是否设置、base_url/model 是否正确；"
               f"当前 {health['detail']}")
    return line(FAIL, label, detail, fix)


def check_tts() -> dict:
    venv = project.COSYVOICE_VENV / "bin" / "python"
    if not venv.exists():
        return line(FAIL, "CosyVoice 环境", "未安装", f"bash {S}/setup_tts.sh")
    models = list(project.COSYVOICE_MODELS.glob("Fun-CosyVoice3*"))
    if not models:
        return line(FAIL, "CosyVoice 模型", "未下载", f"bash {S}/setup_tts.sh")
    prompt = project.COSYVOICE_REPO / "asset" / "zero_shot_prompt.wav"
    if not prompt.exists():
        return line(WARN, "参考音色", f"缺少 {prompt}", "重新运行 setup_tts.sh（会重新拉取仓库资源）")
    return line(OK, "CosyVoice 环境", models[-1].name)


def check_fonts() -> dict:
    fc = shutil.which("fc-list")
    if fc:
        out = subprocess.run([fc], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout
        for name in ("PingFang SC", "Hiragino Sans GB", "STHeiti"):
            if name.lower() in out.lower():
                return line(OK, "字幕字体", name)
        return line(WARN, "字幕字体", "fontconfig 里没有中文字体",
                    "安装任意中文字体，或在 script.json 里改 caption.fontFamily")
    if Path("/System/Library/Fonts/PingFang.ttc").exists():
        return line(OK, "字幕字体", "PingFang.ttc（系统内置）")
    return line(WARN, "字幕字体", "未确认中文字体", "安装中文字体或指定 caption.fontFamily")


def check_node() -> dict:
    node = shutil.which("node")
    if not node:
        return line(FAIL, "Node.js", "未安装", "brew install node（用于抓取参考片）")
    mods = project.TOOLS / "node" / "node_modules" / "playwright"
    if not mods.exists():
        return line(WARN, "Playwright", "未安装", f"bash {S}/setup.sh --node-only")
    return line(OK, "Node 环境", f"{node}")


def check_dirs() -> dict:
    missing = []
    if not project.MATERIAL_DIR.is_dir():
        missing.append(f"素材目录（默认 {project.MATERIAL_DIR.name}/）")
    if not project.REFERENCE_DIR.is_dir():
        missing.append(f"参考片目录（默认 {project.REFERENCE_DIR.name}/）")
    if missing:
        return line(WARN, "工作目录", "缺少：" + "、".join(missing), "建目录或设置 VIDEO_PIPELINE_* 环境变量")
    return line(OK, "工作目录", f"{project.ROOT}")


def check_hardware() -> dict:
    import os
    import shutil as sh
    import subprocess as sp

    chip = sp.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                  capture_output=True, text=True).stdout.strip() or "未知芯片"
    model = sp.run(["sysctl", "-n", "hw.model"],
                   capture_output=True, text=True).stdout.strip()
    # MacBook Air 是无风扇机型，长时间满载会降频
    air_models = {"Mac14,2", "Mac14,15", "Mac15,12", "Mac15,13", "Mac16,12", "Mac16,13"}
    is_air = model.startswith("MacBookAir") or model in air_models
    mem_gb = round(int(sp.run(["sysctl", "-n", "hw.memsize"],
                              capture_output=True, text=True).stdout.strip() or 0) / 1024**3)
    free_gb = round(sh.disk_usage(str(project.ROOT)).free / 1024**3)
    cores = os.cpu_count() or 0

    detail = f"{chip} / {mem_gb}GB / {cores} 核 / 剩余 {free_gb}GB"
    if free_gb < 20:
        return line(WARN, "硬件", detail, "建议至少留 20GB：依赖约 11GB，素材和成片还要空间")
    if mem_gb < 16:
        return line(WARN, "硬件", detail,
                    "内存偏小：建议在 pipeline.conf 里设 fast_analysis=1、segments_per_clip=4、tts_threads=4")
    if is_air:
        return line(WARN, "硬件", detail,
                    "无风扇机型：首次分析建议插电、关掉其他大应用，避免降频拖慢")
    return line(OK, "硬件", detail)


def main() -> None:
    print("\n视频流水线环境自检\n" + "─" * 46)
    results = [check_hardware(), check_ffmpeg(), check_python(), check_node(), check_vlm(),
               check_tts(), check_fonts(), check_dirs()]
    fails = [r for r in results if r["mark"] == FAIL]
    warns = [r for r in results if r["mark"] == WARN]
    print("─" * 46)
    print(f"通过 {sum(1 for r in results if r['mark'] == OK)}/{len(results)}，"
          f"警告 {len(warns)}，阻断 {len(fails)}\n")
    if fails:
        print("阻断项未解决前，流水线无法完整运行。\n")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
