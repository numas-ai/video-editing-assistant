#!/usr/bin/env python3
"""tts.py — 端侧配音合成（Fun-CosyVoice3）

输入：script.json（逐句文案）
输出：work/tts/line_XXX.wav + work/tts/manifest.json（含每句真实时长）

说明：
  - 自动切换到 CosyVoice 专用解释器执行（依赖与流水线主环境隔离）
  - 音色按需固化：首次调用某个参考音色时缓存到 spk2info，后续直接复用
  - 全部本地推理，音频不外传

用法：
  python scripts/tts.py --script work/script.json --out work/tts
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import project
from lib.run import Step, log, read_json, write_json

COSYVOICE_PY = project.COSYVOICE_VENV / "bin" / "python"

DEFAULT_PROMPT_TEXT = "You are a helpful assistant.<|endofprompt|>希望你以后能够做的比我还好呦。"


def ensure_interpreter() -> None:
    """用 CosyVoice 专属 venv 重新执行本脚本。"""
    if not COSYVOICE_PY.exists():
        raise SystemExit(
            f"找不到 CosyVoice 环境：{COSYVOICE_PY}\n"
            f"请先运行 scripts/setup_tts.sh"
        )
    current = Path(sys.executable).resolve()
    if current == COSYVOICE_PY.resolve():
        return
    os.execv(str(COSYVOICE_PY), [str(COSYVOICE_PY), str(Path(__file__).resolve()), *sys.argv[1:]])


def resolve_prompt(voice: dict) -> tuple[str, Path]:
    prompt_text = voice.get("promptText") or DEFAULT_PROMPT_TEXT
    raw = voice.get("promptWav") or "asset/zero_shot_prompt.wav"
    p = Path(raw)
    if not p.is_absolute():
        candidates = [project.COSYVOICE_REPO / raw, project.COSYVOICE_HOME / "voices" / p.name]
        p = next((c for c in candidates if c.exists()), candidates[0])
    if not p.exists():
        raise SystemExit(
            f"找不到参考音色文件：{p}\n"
            f"请把 10-30 秒干净人声放到 {project.COSYVOICE_HOME / 'voices'}，或在 script.json 里指定 voice.promptWav"
        )
    return prompt_text, p


def model_dir() -> Path:
    dirs = sorted(project.COSYVOICE_MODELS.glob("Fun-CosyVoice3*"))
    if not dirs:
        raise SystemExit(f"找不到 CosyVoice3 模型，请先运行 scripts/setup_tts.sh（{project.COSYVOICE_MODELS}）")
    return dirs[-1]


def synthesize(script: dict, out_dir: Path, *, voice: dict) -> dict:
    import torch
    import torchaudio

    sys.path.insert(0, str(project.COSYVOICE_REPO))
    sys.path.append(str(project.COSYVOICE_REPO / "third_party" / "Matcha-TTS"))
    torch.set_num_threads(int(os.environ.get("COSYVOICE_THREADS", str(project.TTS_THREADS))))

    prompt_text, prompt_wav = resolve_prompt(voice)
    spk_id = voice.get("spkId") or "default"

    with Step("tts/load") as s:
        from cosyvoice.cli.cosyvoice import CosyVoice3
        cv = CosyVoice3(str(model_dir()))
        if spk_id not in cv.list_available_spks():
            cv.add_zero_shot_spk(prompt_text, str(prompt_wav), spk_id)
            cv.save_spkinfo()
        log(s.name, f"音色 {spk_id} 就绪，采样率 {cv.sample_rate}")

    speed = float(voice.get("speed", 1.0))
    lines = script.get("lines", [])
    if not lines:
        raise SystemExit("script.json 里没有 lines")

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"schemaVersion": 1, "sampleRate": cv.sample_rate, "voice": spk_id, "speed": speed, "lines": []}

    for idx, line in enumerate(lines, 1):
        text = (line.get("text") or "").strip()
        if not text:
            raise SystemExit(f"第 {idx} 句文案为空")
        dst = out_dir / f"line_{idx:03d}.wav"
        with Step(f"tts/{idx:02d}") as s:
            chunks = list(cv.inference_zero_shot(
                text, "", "", zero_shot_spk_id=spk_id, stream=False, speed=speed,
            ))
            if not chunks:
                raise RuntimeError(f"合成结果为空：{text}")
            audio = torch.cat([c["tts_speech"] for c in chunks], dim=1)
            torchaudio.save(str(dst), audio, cv.sample_rate)
            dur = audio.shape[1] / cv.sample_rate
            log(s.name, f"{dur:.2f}s  {text}")

        manifest["lines"].append({
            "id": line.get("id", idx),
            "text": text,
            "file": str(dst.relative_to(project.ROOT)),
            "duration": round(dur, 3),
            "intent": line.get("intent"),
            "shotHint": line.get("shotHint"),
        })

    manifest["totalDuration"] = round(sum(l["duration"] for l in manifest["lines"]), 3)
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", default=str(project.WORK / "script.json"))
    ap.add_argument("--out", default=str(project.WORK / "tts"))
    args = ap.parse_args()

    ensure_interpreter()

    script = read_json(Path(args.script))
    voice = script.get("voice", {})
    manifest = synthesize(script, Path(args.out), voice=voice)

    write_json(Path(args.out) / "manifest.json", manifest)
    log("tts", f"{len(manifest['lines'])} 句，总长 {manifest['totalDuration']:.1f}s → {Path(args.out)/'manifest.json'}")


if __name__ == "__main__":
    main()
