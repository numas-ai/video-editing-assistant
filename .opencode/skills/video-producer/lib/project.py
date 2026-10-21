"""路径与配置解析。

所有硬编码路径集中在这里，其余脚本一律通过本模块取路径，
保证换机器/换项目目录时只需改环境变量。
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


def _find_root(start: Path) -> Path:
    """向上查找包含 .opencode 目录的项目根。"""
    cur = start.resolve()
    for _ in range(8):
        if (cur / ".opencode").is_dir():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    raise RuntimeError(f"找不到项目根（应包含 .opencode 目录），起点：{start}")


ROOT = Path(os.environ.get("VIDEO_PIPELINE_ROOT") or _find_root(Path(__file__).parent))
SKILL_DIR = ROOT / ".opencode" / "skills" / "video-producer"
LIB_DIR = SKILL_DIR / "lib"
TOOLS = ROOT / ".tools"


def _read_conf(path: Path) -> dict:
    """读取项目根的可读配置（pipeline.conf）。

    格式是最朴素的 key=value，支持 # 注释，不需要任何依赖——
    目的是让不懂技术的使用者能直接打开看懂、改目录。
    """
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip().strip("\"'")
    return data


CONF = _read_conf(ROOT / "pipeline.conf")


def _dir(env_key: str, conf_key: str, default: str) -> Path:
    explicit = os.environ.get(env_key)
    if explicit:
        return Path(explicit)
    return ROOT / CONF.get(conf_key, default)


WORK = _dir("VIDEO_PIPELINE_WORK", "work_dir", "work")
OUTPUT = _dir("VIDEO_PIPELINE_OUTPUT", "output_dir", "output")
REFERENCE_DIR = _dir("VIDEO_PIPELINE_REFERENCE", "reference_dir", "参考视频")
MATERIAL_DIR = _dir("VIDEO_PIPELINE_MATERIAL", "material_dir", "学习素材")

# 默认画幅：source=跟素材比例不裁切；也可以写 9:16 / 3:4 这类比例
DEFAULT_CANVAS = CONF.get("canvas", "source")
DEFAULT_HEIGHT = int(CONF.get("height", "1440"))
DEFAULT_FPS = int(CONF.get("fps", "30"))

# 低配机器（无风扇 / 小内存 / 少核心）用这些开关换取可接受的等待时间
SEGMENTS_PER_CLIP = int(CONF.get("segments_per_clip", "8"))
FAST_ANALYSIS = CONF.get("fast_analysis", "0").strip().lower() in ("1", "true", "yes", "on")
TTS_THREADS = int(CONF.get("tts_threads", "8"))

TESSDATA = TOOLS / "tessdata-best"
COSYVOICE_HOME = TOOLS / "cosyvoice"
COSYVOICE_VENV = COSYVOICE_HOME / "venv"
COSYVOICE_REPO = COSYVOICE_HOME / "repo"
COSYVOICE_MODELS = COSYVOICE_HOME / "models"

def _resolve_tool(env_key: str, candidates: list[str]) -> str:
    explicit = os.environ.get(env_key)
    if explicit:
        return explicit
    for c in candidates:
        if "/" in c:
            if Path(c).exists():
                return c
        else:
            found = shutil.which(c)
            if found:
                return found
    raise RuntimeError(
        f"找不到 {env_key} 指定的程序，候选：{candidates}\n"
        f"请运行 scripts/setup.sh，或通过环境变量显式指定。"
    )


@dataclass(frozen=True)
class Tools:
    ffmpeg: str
    ffprobe: str


def tools() -> Tools:
    """ffmpeg 优先用 ffmpeg-full（需 libass/drawtext），退回 PATH 里的 ffmpeg。"""
    ffmpeg = _resolve_tool(
        "VIDEO_PIPELINE_FFMPEG",
        ["/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg", "ffmpeg"],
    )
    ffprobe = os.environ.get("VIDEO_PIPELINE_FFPROBE") or str(
        Path(ffmpeg).with_name("ffprobe")
    )
    if not Path(ffprobe).exists():
        ffprobe = _resolve_tool("VIDEO_PIPELINE_FFPROBE", ["ffprobe"])
    return Tools(ffmpeg=ffmpeg, ffprobe=ffprobe)


def ensure_dirs() -> None:
    for d in (WORK, OUTPUT):
        d.mkdir(parents=True, exist_ok=True)


def require_pipeline_python() -> str:
    """返回流水线 venv 的 python；若当前解释器已具备依赖则返回自己。"""
    try:
        import numpy  # noqa: F401
        import PIL  # noqa: F401
        return sys.executable
    except ImportError:
        pass
    exe = TOOLS / "py" / "pipeline" / "bin" / "python"
    if exe.exists():
        return str(exe)
    raise RuntimeError(
        "缺少流水线 Python 依赖（numpy/pillow）。请运行 scripts/setup.sh。"
    )
