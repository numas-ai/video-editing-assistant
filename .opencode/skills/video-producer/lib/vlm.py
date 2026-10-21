"""视觉模型调用层（可插拔：生产级云端 / 本地端侧）

设计原则：
  - **VL 走生产级云端**（识别粒度决定画面与口播能不能对上）
  - **TTS 保持端侧**（本地推理，不上传素材）
  - 密钥只从环境变量读，绝不写进仓库

支持两种后端：
  openai  —— 任何 OpenAI 兼容的视觉接口（火山方舟 / 阿里百炼 / OpenAI / Gemini 兼容层）
  ollama  —— 本地模型，仅在离线或降级时使用

配置优先级：环境变量 > pipeline.conf > 默认值

环境变量：
  VIDEO_PIPELINE_VLM_PROVIDER   openai | ollama
  VIDEO_PIPELINE_VLM_BASE_URL   例：https://ark.cn-beijing.volces.com/api/v3
  VIDEO_PIPELINE_VLM_MODEL      例：doubao-1.5-vision-pro-32k-250115
  VIDEO_PIPELINE_VLM_API_KEY    密钥（或通过 vlm_api_key_env 指定变量名）
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from . import project


class VLMError(RuntimeError):
    pass


def _setting(env_key: str, conf_key: str, default: str) -> str:
    return os.environ.get(env_key) or project.CONF.get(conf_key, default)


PROVIDER = _setting("VIDEO_PIPELINE_VLM_PROVIDER", "vlm_provider", "ollama").strip().lower()
BASE_URL = _setting("VIDEO_PIPELINE_VLM_BASE_URL", "vlm_base_url", "http://127.0.0.1:11434").rstrip("/")
MODEL = _setting("VIDEO_PIPELINE_VLM_MODEL", "vlm_model", "qwen3-vl:4b-instruct")
KEY_ENV_NAME = _setting("VIDEO_PIPELINE_VLM_API_KEY_ENV", "vlm_api_key_env", "VLM_API_KEY")
API_KEY = os.environ.get("VIDEO_PIPELINE_VLM_API_KEY") or os.environ.get(KEY_ENV_NAME, "")
MAX_SIDE = int(_setting("VIDEO_PIPELINE_VLM_MAX_SIDE", "vlm_max_side", "896"))
CONCURRENCY = int(_setting("VIDEO_PIPELINE_VLM_CONCURRENCY", "vlm_concurrency", "4"))
RETRIES = int(_setting("VIDEO_PIPELINE_VLM_RETRIES", "vlm_retries", "3"))

# 缓存命名空间：换模型必须换缓存，否则会复用旧模型的标注
MODEL_TAG = re.sub(r"[^a-zA-Z0-9._-]+", "-", MODEL)[:40]


# ---------------------------------------------------------------- 编码


def _encode(image: Path | str) -> str:
    with Image.open(image) as im:
        im = im.convert("RGB")
        im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ---------------------------------------------------------------- 后端


def _post_json(url: str, payload: dict, headers: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ask_openai(b64: str, prompt: str, timeout: int) -> str:
    if not API_KEY:
        raise VLMError(
            f"缺少视觉模型密钥。请设置环境变量 {KEY_ENV_NAME}。\n"
            f"  当前后端：{PROVIDER} / {MODEL}\n"
            f"  例：export {KEY_ENV_NAME}=你的密钥"
        )
    data = _post_json(
        f"{BASE_URL}/chat/completions",
        {
            "model": MODEL,
            "temperature": 0,
            "max_tokens": 300,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
        },
        {"Authorization": f"Bearer {API_KEY}"},
        timeout,
    )
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise VLMError(f"返回格式异常：{json.dumps(data, ensure_ascii=False)[:400]}") from exc


def _ask_ollama(b64: str, prompt: str, timeout: int) -> str:
    data = _post_json(
        f"{BASE_URL}/api/generate",
        {
            "model": MODEL,
            "prompt": prompt,
            "images": [b64],
            "stream": False,
            "options": {"temperature": 0, "num_predict": 256},
        },
        {},
        timeout,
    )
    return (data.get("response") or "").strip()


def _dispatch(b64: str, prompt: str, timeout: int) -> str:
    if PROVIDER == "ollama":
        return _ask_ollama(b64, prompt, timeout)
    return _ask_openai(b64, prompt, timeout)


def ask(image: Path | str, prompt: str, *, timeout: int = 120) -> str:
    """单张图提问。失败会重试，最终失败抛 VLMError（不静默降级）。"""
    b64 = _encode(image)
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            return _dispatch(b64, prompt, timeout)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "ignore")[:300]
            last = VLMError(f"HTTP {exc.code}：{body}")
            if exc.code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            break
        except (urllib.error.URLError, TimeoutError, VLMError) as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise VLMError(f"视觉模型调用失败（{PROVIDER}/{MODEL}）：{last}")


def ask_many(images: list[Path | str], prompt: str, *,
             concurrency: int | None = None, timeout: int = 120) -> list[str]:
    """并发批量提问。用于素材逐片段标注这种几百次的场景。"""
    if not images:
        return []
    workers = max(1, concurrency or CONCURRENCY)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda p: ask(p, prompt, timeout=timeout), images))


def health() -> dict:
    """检查后端可用性。返回 ok / provider / model / detail。"""
    if PROVIDER == "ollama":
        try:
            with urllib.request.urlopen(f"{BASE_URL}/api/tags", timeout=5) as resp:
                names = [m.get("name", "") for m in json.loads(resp.read()).get("models", [])]
            return {"ok": MODEL in names, "provider": PROVIDER, "model": MODEL,
                    "detail": f"本地模型列表 {len(names)} 个"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "provider": PROVIDER, "model": MODEL, "detail": str(exc)}

    if not API_KEY:
        return {"ok": False, "provider": PROVIDER, "model": MODEL,
                "detail": f"未设置密钥环境变量 {KEY_ENV_NAME}"}
    try:
        req = urllib.request.Request(
            f"{BASE_URL}/models", headers={"Authorization": f"Bearer {API_KEY}"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        names = [m.get("id", "") for m in data.get("data", [])]
        return {"ok": True, "provider": PROVIDER, "model": MODEL,
                "detail": f"可用模型 {len(names)} 个" if names else "接口可达"}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "provider": PROVIDER, "model": MODEL,
                "detail": f"HTTP {exc.code}（密钥无效或模型未开通）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "provider": PROVIDER, "model": MODEL, "detail": str(exc)}


# ---------------------------------------------------------------- 提示词


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


# ---------------------------------------------------------------- 语义封装


def ocr(image: Path | str) -> str:
    text = ask(image, OCR_PROMPT).strip()
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
    raw = ask(image, LABEL_PROMPT)
    return next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")


def label_many(images: list[Path | str]) -> list[str]:
    raws = ask_many(images, LABEL_PROMPT)
    out = []
    for raw in raws:
        first = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
        out.append(first)
    return out


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
