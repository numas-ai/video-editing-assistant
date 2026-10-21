#!/usr/bin/env bash
# setup_tts.sh — 安装端侧 TTS（Fun-CosyVoice3-0.5B）运行环境
#
# 设计：独立 venv + 仓库副本 + 模型权重全部收在 .tools/cosyvoice 下，
#       不污染系统 Python，也不影响主流水线环境（可独立重装/降级）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
CV="$ROOT/.tools/cosyvoice"
REPO="$CV/repo"
VENV="$CV/venv"
MODELS="$CV/models"
MODEL_ID="${COSYVOICE_MODEL_ID:-FunAudioLLM/Fun-CosyVoice3-0.5B-2512}"
MODEL_DIR="$MODELS/$(basename "$MODEL_ID")"

log() { printf '[setup-tts] %s\n' "$*"; }

mkdir -p "$CV" "$MODELS"

# 1) 仓库
if [ -d "$REPO/.git" ]; then
  log "更新仓库"
  git -C "$REPO" fetch --depth 1 origin >/dev/null 2>&1 || true
  git -C "$REPO" submodule update --init --recursive >/dev/null
else
  log "克隆仓库（含子模块）"
  git clone --recursive --depth 1 https://github.com/FunAudioLLM/CosyVoice.git "$REPO"
fi

# 2) Python 环境
if [ ! -x "$VENV/bin/python" ]; then
  log "创建 Python 3.10 venv"
  uv venv --python 3.10 "$VENV"
fi

PY="$VENV/bin/python"
uv pip install --python "$PY" -q \
  "torch==2.3.1" "torchaudio==2.3.1" \
  "transformers==4.51.3" \
  "HyperPyYAML==1.2.3" "omegaconf==2.3.0" \
  "conformer==0.3.2" "diffusers==0.29.0" "x-transformers==2.11.24" \
  "librosa==0.10.2" "numpy==1.26.4" "soundfile==0.12.1" \
  "onnxruntime==1.18.0" "wetext==0.0.4" "inflect==7.3.1" \
  "matplotlib==3.7.5" "tqdm" "modelscope" "huggingface_hub"

# 3) 模型权重
if [ ! -f "$MODEL_DIR/cosyvoice3.yaml" ] && [ ! -f "$MODEL_DIR/cosyvoice2.yaml" ]; then
  log "下载模型权重 ${MODEL_ID}（约 2GB，首次较慢）"
  "$PY" - "$MODEL_DIR" "$MODEL_ID" <<'PY'
import sys
from modelscope import snapshot_download
snapshot_download(sys.argv[2], local_dir=sys.argv[1])
PY
else
  log "模型权重已存在，跳过下载"
fi

# 4) 冒烟测试
log "冒烟测试"
cd "$REPO"
PYTHONPATH="$REPO:$REPO/third_party/Matcha-TTS" "$PY" - "$MODEL_DIR" <<'PY'
import sys, glob, os
from cosyvoice.cli.cosyvoice import CosyVoice3
d = sys.argv[1]
yaml = next(iter(glob.glob(os.path.join(d, '*.yaml'))), None)
print('model yaml:', yaml)
cv = CosyVoice3(d)
try:
    print('内置音色:', cv.list_available_spks())
except Exception as e:
    print('内置音色接口不可用:', e)
PY

log "完成。环境：$VENV"
