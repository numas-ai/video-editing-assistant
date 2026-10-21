#!/usr/bin/env bash
# bootstrap.sh — 从一台干净 macOS 到"流水线可用"
#
# 目标：让 AI 或人只跑这一条命令，就能把环境装到 doctor 全绿。
# 幂等：已装的东西会跳过；任何一步失败都会给出可执行的下一步。
#
# 用法：
#   bash scripts/bootstrap.sh              # 全装（含本地 TTS，约 6GB）
#   bash scripts/bootstrap.sh --no-tts     # 不装 TTS（先用系统占位/后续再装）
#   bash scripts/bootstrap.sh --dry-run    # 只打印将要做什么，不动系统
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
S="$ROOT/.opencode/skills/video-producer/scripts"
PY="$ROOT/.tools/py/pipeline/bin/python"
DRY=0
WITH_TTS=1
VLM_MODEL="${VIDEO_PIPELINE_VLM_MODEL:-qwen3-vl:4b-instruct}"

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --no-tts) WITH_TTS=0 ;;
    -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "未知参数：$arg"; exit 2 ;;
  esac
done

log()  { printf '\033[1m[bootstrap]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[bootstrap] %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m[bootstrap] %s\033[0m\n' "$*"; exit 1; }
run()  { if [ "$DRY" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }
has()  { command -v "$1" >/dev/null 2>&1; }

[ "$(uname -s)" = "Darwin" ] || fail "当前只支持 macOS（检测到 $(uname -s)）"

# 网络抖动很常见，下载类操作统一重试三次
retry() {
  local tries=3
  for i in $(seq 1 $tries); do
    "$@" && return 0
    [ "$i" -lt "$tries" ] && { warn "第 $i 次失败，3 秒后重试：$*"; sleep 3; }
  done
  return 1
}

# 快路径：环境已就绪就不折腾（避免每次会话都跑一遍安装）
if [ "$DRY" = 0 ] && [ -x "$PY" ]; then
  if "$PY" "$S/doctor.py" >/dev/null 2>&1; then
    log "环境已就绪，跳过安装"
    "$PY" "$S/doctor.py"
    exit 0
  fi
fi

log "预计耗时 30–50 分钟（主要在下模型），占用约 11GB"

# 视觉模型走云端时不需要装 Ollama；只有 provider=ollama 才装
CONF="$ROOT/pipeline.conf"
VLM_PROVIDER="$( [ -f "$CONF" ] && grep -E '^vlm_provider=' "$CONF" | head -1 | cut -d= -f2 | tr -d ' ' || true )"
VLM_PROVIDER="${VLM_PROVIDER:-ollama}"

# ---------------------------------------------------------------- 1. Homebrew
if ! has brew; then
  log "安装 Homebrew —— 系统可能会要求输入一次 Mac 开机密码（终端里不会显示字符，输完回车）"
  run /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  if [ "$DRY" = 0 ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)" || fail "Homebrew 装完但无法加载，请重开终端后重跑"
  fi
else
  log "Homebrew 已就位"
fi

# ---------------------------------------------------------------- 2. 基础工具
missing=()
has ffmpeg || true
[ -x /opt/homebrew/opt/ffmpeg-full/bin/ffmpeg ] || missing+=("ffmpeg-full")
has uv   || missing+=("uv")
has node || missing+=("node")
if [ ${#missing[@]} -gt 0 ]; then
  log "安装：${missing[*]}"
  retry brew install "${missing[@]}"
else
  log "ffmpeg-full / uv / node 已就位"
fi

# ---------------------------------------------------------------- 3. Ollama + 视觉模型
if [ "$VLM_PROVIDER" = "ollama" ]; then
if ! has ollama; then
  log "安装 Ollama"
  retry brew install ollama
fi

if [ "$DRY" = 1 ]; then
  log "[dry-run] 检查 Ollama 服务；未启动则 ollama serve；缺模型则 ollama pull ${VLM_MODEL}（约 3.3GB）"
else
  if ! curl -s -m 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    log "启动 Ollama 服务"
    (ollama serve >/tmp/ollama-bootstrap.log 2>&1 &) || warn "Ollama 启动失败，详见 /tmp/ollama-bootstrap.log"
    for _ in $(seq 1 20); do
      curl -s -m 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
      sleep 1
    done
  fi
  if curl -s -m 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    if ! ollama list 2>/dev/null | grep -q "${VLM_MODEL%%:*}"; then
      log "拉取视觉模型 $VLM_MODEL（约 3.3GB）"
      retry ollama pull "$VLM_MODEL"
    else
      log "视觉模型已存在"
    fi
  else
    warn "Ollama 未响应，跳过模型拉取；可稍后手动执行：ollama serve && ollama pull $VLM_MODEL"
  fi
fi
else
  log "视觉模型走云端（${VLM_PROVIDER}），跳过 Ollama 安装"
fi

# ---------------------------------------------------------------- 4. 流水线依赖
log "安装流水线 Python / Node 依赖"
run bash "$S/setup.sh" --python-only
run bash "$S/setup.sh" --node-only

# ---------------------------------------------------------------- 5. 本地 TTS
if [ "$WITH_TTS" = 1 ]; then
  log "安装本地 TTS（约 6GB，首次较慢）"
  run bash "$S/setup_tts.sh"
else
  warn "按参数跳过 TTS；此时 plan_edit 仍可跑，但 tts.py 会失败"
fi

# ---------------------------------------------------------------- 6. 自检
log "运行环境自检"
if [ "$DRY" = 1 ]; then
  echo "  [dry-run] $PY $S/doctor.py"
  exit 0
fi

set +e
"$PY" "$S/doctor.py"
rc=$?
set -e
if [ $rc -ne 0 ]; then
  fail "自检未通过，按上面每条 → 后面的命令处理后重跑 bootstrap.sh"
fi

log "环境就绪。下一步：node $S/fetch_reference.mjs --url <抖音链接>"
