"""本地视觉模型（Ollama）调用封装。

用途：读画面文案、描述镜头内容、判断镜头类型。
全部本地推理，不联网，不上传素材。
"""
from __future__ import annotations

import base64
import re
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import project
from .run import log


class VLMError(RuntimeError):
    pass


def _post(payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        f"{project.VLM_URL}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise VLMError(
            f"无法连接本地视觉模型 {project.VLM_URL}：{exc.reason}\n"
            f"请确认 Ollama 已启动，且已拉取模型 {project.VLM_MODEL}。"
        ) from exc


def health() -> dict:
    try:
        with urllib.request.urlopen(f"{project.VLM_URL}/api/tags", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        names = [m.get("name", "") for m in data.get("models", [])]
        return {"ok": project.VLM_MODEL in names, "models": names}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "models": [], "error": str(exc)}


def ask(image: Path | str, prompt: str, *, retries: int = 2, timeout: int = 180) -> str:
    b64 = base64.b64encode(Path(image).read_bytes()).decode("ascii")
    payload = {
        "model": project.VLM_MODEL,
        "prompt": prompt,
        "images": [b64],
        "stream": False,
        "options": {"temperature": 0, "num_predict": 256},
    }
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            data = _post(payload, timeout)
            return (data.get("response") or "").strip()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise VLMError(f"视觉模型调用失败：{last}")


OCR_PROMPT = "只输出图片中出现的文字内容，按阅读顺序用空格分隔。没有文字就输出 EMPTY。不要解释，不要加标点以外的字符。"

DESCRIBE_PROMPT = (
    "用一句中文描述这张图。必须包含：主体物品与颜色、构图类型"
    "（产品特写/平铺展示/手持展示/真人上身/包装展示/其他）、背景环境。"
    "不超过 40 字，不要解释。"
)

CLASSIFY_PROMPT = (
    "判断这张图的构图类型，只输出以下之一：product_closeup, flatlay, handheld, "
    "model_wear, package, other。不要解释。"
)

LABEL_PROMPT = (
    "看这张商品展示图，按三段回答，用 | 分隔，不要解释，不要换行：\n"
    "主体：颜色+款式，如“棕色三角杯内衣”“豹纹内衣”“裸色杯垫”“透明包装袋”；\n"
    "动作：手在做什么，如“平铺陈列”“手托正面”“翻到背面”“扣合前扣”“拉扯肩带”“装入包装袋”“多款并排对比”；\n"
    "细节：画面里能清楚看到的局部，从这些词里选最多三个：前扣、肩带、杯垫、后背、侧边、底围、浅蓝边、豹纹、包装袋、多款。看不到就写“无”。\n"
    "格式示例：棕色三角杯内衣 | 手托正面 | 前扣,浅蓝边"
)


def ocr(image: Path | str) -> str:
    text = ask(image, OCR_PROMPT)
    text = text.strip()
    if text.upper().startswith("EMPTY"):
        return ""
    return " ".join(text.split())


def describe(image: Path | str) -> str:
    return ask(image, DESCRIBE_PROMPT).replace("\n", " ").strip()


def classify(image: Path | str) -> str:
    raw = ask(image, CLASSIFY_PROMPT).strip().lower()
    for key in ("product_closeup", "flatlay", "handheld", "model_wear", "package", "other"):
        if key in raw:
            return key
    return "other"


def label_segment(image: Path | str) -> str:
    """返回“主体|动作|细节”格式的标注。"""
    raw = ask(image, LABEL_PROMPT, timeout=90)
    first = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
    return first


def split_label(label: str) -> tuple[str, str, str]:
    """把“主体|动作|细节”拆成三段；模型常用空格或多段，做容错。"""
    if not label:
        return "", "", ""
    text = label.replace("｜", "|").strip()
    if "|" in text:
        parts = [p.strip() for p in text.split("|")]
    else:
        parts = [p for p in re.split(r"\s{2,}", text) if p.strip()]
        if len(parts) < 3:
            parts = text.split(" ", 2)
    parts = [p for p in parts if p]
    while len(parts) < 3:
        parts.append("")
    if parts[2].strip() in ("无", "None", "", "-"):
        parts[2] = ""
    return parts[0].strip(), parts[1].strip(), parts[2].strip()
