#!/usr/bin/env bash
# setup.sh — 一条命令把流水线依赖装齐（macOS / Apple Silicon）
#
# 分段执行，可单独重跑：
#   bash scripts/setup.sh              # 全部
#   bash scripts/setup.sh --python-only
#   bash scripts/setup.sh --node-only
#   bash scripts/setup.sh --ffmpeg-only
#   SOURCE_TTS=1 bash scripts/setup.sh  # 顺带装好本地 TTS
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SKILL="$ROOT/.opencode/skills/video-producer"
TOOLS="$ROOT/.tools"

log() { printf '[setup] %s\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { log "缺少 $1，请先安装（brew install $1）"; exit 1; }; }

MODE="${1:-all}"

# ---------------------------------------------------------------- ffmpeg
if [ "$MODE" = "all" ] || [ "$MODE" = "--ffmpeg-only" ]; then
  if [ ! -x /opt/homebrew/opt/ffmpeg-full/bin/ffmpeg ]; then
    log "安装 ffmpeg-full（含 libass / drawtext / xfade）"
    brew install ffmpeg-full
  else
    log "ffmpeg-full 已就位"
  fi
fi

# ---------------------------------------------------------------- python
if [ "$MODE" = "all" ] || [ "$MODE" = "--python-only" ]; then
  need uv
  mkdir -p "$TOOLS/py"
  if [ ! -x "$TOOLS/py/pipeline/bin/python" ]; then
    log "创建流水线 Python 环境"
    uv venv --python 3.11 "$TOOLS/py/pipeline"
  fi
  log "安装流水线依赖"
  uv pip install --python "$TOOLS/py/pipeline/bin/python" -q \
    numpy scipy librosa soundfile pillow pillow-heif pyyaml jsonschema
fi

# ---------------------------------------------------------------- node
if [ "$MODE" = "all" ] || [ "$MODE" = "--node-only" ]; then
  need node
  mkdir -p "$TOOLS/node"
  if [ ! -f "$TOOLS/node/package.json" ]; then
    cat > "$TOOLS/node/package.json" <<'JSON'
{
  "name": "video-pipeline-tools",
  "private": true,
  "type": "module",
  "dependencies": { "playwright": "^1.61.1" }
}
JSON
  fi
  log "安装 Playwright 与 Chromium"
  (cd "$TOOLS/node" && npm install --no-audit --no-fund && npx playwright install chromium)
fi

# ---------------------------------------------------------------- tessdata
if [ "$MODE" = "all" ] || [ "$MODE" = "--python-only" ]; then
  if [ ! -f "$TOOLS/tessdata-best/chi_sim.traineddata" ]; then
    log "下载中文 OCR 语言包（备选方案，主链路用视觉模型）"
    mkdir -p "$TOOLS/tessdata-best"
    curl -sL -o "$TOOLS/tessdata-best/chi_sim.traineddata" \
      https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/main/chi_sim.traineddata
  fi
fi

# ---------------------------------------------------------------- tts
if [ "${SOURCE_TTS:-0}" = "1" ]; then
  log "安装本地 TTS"
  bash "$SKILL/scripts/setup_tts.sh"
fi

# ---------------------------------------------------------------- 目录
mkdir -p "$ROOT/work" "$ROOT/output" "$ROOT/参考视频" "$ROOT/学习素材"

log "完成。自检："
"$TOOLS/py/pipeline/bin/python" "$SKILL/scripts/doctor.py" || true
