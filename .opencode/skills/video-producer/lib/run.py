"""统一日志与子进程执行。"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


class Step:
    """阶段日志：开始/成功/失败都落到 stderr，便于逐阶段排障。"""

    def __init__(self, name: str):
        self.name = name
        self.t0 = time.time()

    def __enter__(self):
        log(self.name, "开始")
        return self

    def __exit__(self, exc_type, exc, tb):
        dt = time.time() - self.t0
        if exc_type:
            log(self.name, f"失败（{dt:.1f}s）：{exc}", level="error")
            return False
        log(self.name, f"完成（{dt:.1f}s）")
        return False


def log(step: str, msg: str, level: str = "info") -> None:
    prefix = {"info": "", "warn": "⚠ ", "error": "✗ "}.get(level, "")
    sys.stderr.write(f"[{step}] {prefix}{msg}\n")
    sys.stderr.flush()


def run(cmd: list[str], *, step: str = "exec", capture: bool = True, timeout: int | None = None) -> str:
    """执行命令；失败时把 stderr 尾部带进异常，避免静默。"""
    started = time.time()
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    dt = time.time() - started
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-2000:]
        raise RuntimeError(
            f"[{step}] 命令失败（exit {proc.returncode}, {dt:.1f}s）\n"
            f"cmd: {' '.join(cmd[:12])}{' ...' if len(cmd) > 12 else ''}\n"
            f"stderr:\n{tail}"
        )
    return proc.stdout or ""


def run_json(cmd: list[str], *, step: str = "exec", timeout: int | None = None) -> dict:
    out = run(cmd, step=step, timeout=timeout)
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"[{step}] 期望 JSON 输出，实际得到：{out[:500]}") from exc


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"缺少输入文件：{path}（请先运行上一阶段）")
    return json.loads(path.read_text(encoding="utf-8"))
