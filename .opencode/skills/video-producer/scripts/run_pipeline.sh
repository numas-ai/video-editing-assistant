#!/usr/bin/env bash
# run_pipeline.sh — 按顺序跑完整条链路
#
# 用法：
#   bash scripts/run_pipeline.sh                       # 用已有参考片，成片名 final
#   bash scripts/run_pipeline.sh "<抖音链接>" 我的成片   # 先抓参考片再跑
#
# 前置：work/script.json 已写好（文案是创作环节，不在自动流程里生成）
# 幂等：各阶段产物落在 work/ 下，视觉模型结果有缓存，重复执行不会重复烧推理成本
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
S="$ROOT/.opencode/skills/video-producer/scripts"
PY="$ROOT/.tools/py/pipeline/bin/python"

REF_URL="${1:-}"
NAME="${2:-final}"

cd "$ROOT"

step() { printf '\n\033[1m▶ %s\033[0m\n' "$1"; }

if [ -n "$REF_URL" ]; then
  step "1/7 抓取参考片"
  node "$S/fetch_reference.mjs" --url "$REF_URL" --out 参考视频
else
  step "1/7 跳过抓取（使用已有 参考视频/reference.mp4）"
  [ -f "$ROOT/参考视频/reference.mp4" ] || {
    echo "缺少 参考视频/reference.mp4，请传抖音链接或手动放入文件" >&2
    exit 1
  }
fi

step "2/7 环境自检"
"$PY" "$S/doctor.py"

step "3/7 拆解参考片"
"$PY" "$S/analyze_reference.py"

step "4/7 素材体检"
"$PY" "$S/analyze_material.py"

step "5/7 配音合成"
"$PY" "$S/tts.py"

step "6/7 编排并渲染"
"$PY" "$S/plan_edit.py"
"$PY" "$S/render.py" --name "$NAME"

step "7/9 成片质检"
"$PY" "$S/qc.py" --input "$ROOT/output/$NAME.mp4"

step "8/9 存档这一版（永不覆盖）"
"$PY" "$S/version.py" save --video "$ROOT/output/$NAME.mp4" --note "run_pipeline 自动保存"

step "9/9 生成审阅证据"
"$PY" "$S/review.py" --input "$ROOT/output/$NAME.mp4"

printf '\n\033[1m待验收：%s\033[0m\n' "$ROOT/output/$NAME.mp4"
printf '审阅证据：%s\n' "$ROOT/work/review/"
printf '质检报告：%s\n' "$ROOT/work/qc/qc-report.json"
printf '\n请按 SKILL.md 的三道闸门自检，再用 question 向用户确认是否可交付。\n'
