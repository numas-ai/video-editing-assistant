#!/usr/bin/env python3
"""plan_edit.py — 把"文案 + 素材 + 风格"编译成可渲染的剪辑时间线

设计原则：时间线是数据，不是代码。
  edit-plan.json 完整描述每个镜头的来源、入出点、裁切、调色、转场、字幕与音轨，
  渲染器只负责忠实执行，因此可以 diff、可以局部重跑、可以人工微调后再渲染。

两个关键编辑决策：
  1. 一句口播不一定只对应一个镜头。素材不够长时拆成多个机位，而不是硬撑到一个镜头上。
  2. 切点是否落在运动上决定用硬切还是过渡：静止帧硬切最隐形，
     运动中的话（例如手正在把商品收回去）用短溶解接走，画面自然。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from lib import ffmpeg, metrics, project
from lib.run import Step, log, read_json, write_json

# 参考片观感：暖棕、略提饱和、轻微提亮。数值经拆解比对得出，可按需覆盖。
DEFAULT_GRADE = {
    "name": "warm_clean",
    "eq": {"brightness": 0.02, "contrast": 1.04, "saturation": 1.06, "gamma": 0.98},
    "colorbalance": {"rs": 0.02, "gs": 0.0, "bs": -0.02},
}

# 允许越过候选窗口、继续使用的最大秒数。
# 收手动作可以出现，但只给这么长，再长就靠转场接走。
MAX_TAIL_EXTEND = 0.45
MIN_SHOT_SEC = 0.55
DISSOLVE_SEC = 0.28
# 尾部帧间差超过这个值，视为"画面正在动"，用溶解而不是硬切
MOTION_CUT_THRESHOLD = 0.016

# 口播语义 → 画面证据 的匹配规则。
# 关键教训：不能只查"画面里有没有这个词"，因为背景里的其他商品会造成误命中。
# 颜色/花纹必须落在**主体**上，动作必须落在**动作**上，细节只作为加分项。
COLOR_WORDS: dict[str, tuple[str, ...]] = {
    "棕色": ("棕",), "棕": ("棕",),
    "黑色": ("黑",), "黑": ("黑",),
    "豹纹": ("豹纹",),
    "裸色": ("裸色", "米色"), "米色": ("裸色", "米色"),
    "浅蓝": ("浅蓝", "蓝"),
}

ACTION_RULES: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("扣", "一搭", "穿好", "跑位"), ("扣",)),
    (("平铺", "对比", "多款", "配色", "随便挑", "挑", "两件", "价格", "划算", "销量", "左下角", "下单"),
     ("平铺", "对比", "并排", "陈列", "多款")),
    (("包装", "袋"), ("包装", "装袋", "袋")),
    (("翻", "后背", "露背", "美背"), ("翻", "背")),
    (("托", "聚拢", "承托", "胸型", "压"), ("托",)),
    (("底围", "边缘", "走线", "勒肉"), ("底围", "边缘", "浅蓝边")),
    (("肩带", "勒肩"), ("肩带",)),
]

DETAIL_TERMS = ("前扣", "肩带", "杯垫", "后背", "侧边", "底围", "浅蓝边", "豹纹", "包装袋", "多款")


def crop_geometry(sw: int, sh: int, tw: int, th: int, focus_x: int | None = None) -> dict:
    """算出覆盖目标画幅所需的裁切区域，并返回缩放尺寸。

    focus_x 由主体检测给出（源像素坐标），缺省时按中心裁切。
    """
    target_ar = tw / th
    src_ar = sw / sh
    if src_ar > target_ar:
        ch = sh
        cw = int(round(sh * target_ar))
    else:
        cw = sw
        ch = int(round(sw / target_ar))
    cw -= cw % 2
    ch -= ch % 2
    x = (sw - cw) // 2 if focus_x is None else max(0, min(sw - cw, int(focus_x)))
    return {
        "x": x,
        "y": (sh - ch) // 2,
        "w": cw,
        "h": ch,
        "outW": tw,
        "outH": th,
    }


def canvas_for(mode: str, style: dict, index: dict, height: int) -> tuple[int, int]:
    """决定输出画布。

    source    ：跟随素材自身比例，一帧不裁（默认）
    reference ：跟随参考片比例，需要裁切素材
    "9:16"    ：指定比例，按该比例裁切素材
    """
    if mode == "reference":
        ar = style["canvas"]["width"] / style["canvas"]["height"]
    elif ":" in mode:
        try:
            w_ratio, h_ratio = mode.split(":", 1)
            ar = float(w_ratio) / float(h_ratio)
        except (ValueError, ZeroDivisionError):
            raise SystemExit(f"画幅比例格式不对：{mode}，应该像 9:16 或 3:4")
    else:
        ratios = sorted(c["width"] / c["height"] for c in index["clips"] if c.get("height"))
        if not ratios:
            raise SystemExit("素材库里没有可用的视频尺寸信息")
        ar = ratios[len(ratios) // 2]

    h = height - height % 2
    w = int(round(h * ar))
    w -= w % 2
    return w, h


class Picker:
    """在候选片段里做带惩罚的贪心选择。

    优化目标不只是"分数高"，还要"看起来不重复"：
      - 画面指纹与已选镜头越像，扣分越重（避免同一个商品/同一个构图反复出现）
      - 同一素材最多复用 MAX_CLIP_REUSE 次
      - 最近 3 个镜头用过的素材直接排除
    """

    MAX_CLIP_REUSE = 2
    DIVERSITY_WEIGHT = 8.0
    SIM_BASELINE = 0.47      # 同一背景下画面相似度的常态水平，低于它不算重复
    SEMANTIC_WEIGHT = 0.35
    SUBJECT_BONUS = 0.55

    def __init__(self, index: dict, signatures: dict[str, list[float]] | None = None):
        self.segments = []
        self.sigs: dict[str, np.ndarray] = {}
        raw = signatures or {}
        self.segments = []
        for clip in index["clips"]:
            for seg in clip["segments"]:
                item = {
                    **seg,
                    "clipId": clip["id"],
                    "path": clip["path"],
                    "srcW": clip["width"],
                    "srcH": clip["height"],
                    "clipDuration": clip["duration"],
                    "kinds": clip.get("kinds") or [],
                    "description": clip.get("description", ""),
                    # 静态图没有逐帧标注，继承素材级标注
                    "subject": seg.get("subject") or clip.get("subject", ""),
                    "action": seg.get("action") or clip.get("action", ""),
                    "details": seg.get("details") or clip.get("details", ""),
                    "static": clip.get("static", False),
                }
                self.segments.append(item)
                key = f"{clip['id']}@{seg['start']}"
                if key in raw:
                    self.sigs[f"{clip['id']}|{seg['start']}"] = np.asarray(raw[key], dtype=np.float32)

        self.history: list[str] = []
        self.used: set[tuple[str, float]] = set()
        self.usage: dict[str, int] = {}
        self.used_sigs: list[np.ndarray] = []
        self.exhausted_once = False

    def _sig(self, seg: dict) -> np.ndarray | None:
        return self.sigs.get(f"{seg['clipId']}|{seg['start']}")

    def _novelty_penalty(self, seg: dict) -> float:
        sig = self._sig(seg)
        if sig is None or not self.used_sigs:
            return 0.0
        peak = max(float(np.dot(sig, u)) for u in self.used_sigs)
        return self.DIVERSITY_WEIGHT * max(0.0, peak - self.SIM_BASELINE)

    def _semantic_score(self, text: str, seg: dict) -> float:
        """判断这段话和这个镜头的画面在讲同一件事没有。

        三层判断，权重从高到低：
          1. 颜色/花纹必须落在主体上——说"豹纹"就不能配棕色，哪怕背景里有豹纹
          2. 动作要对得上——说"一扣就穿好"就得有扣合动作，不能只是手托着
          3. 细节只做加分——前扣/肩带/底围这些局部特征
        """
        if not text:
            return 0.0
        subject = seg.get("subject") or ""
        action = seg.get("action") or ""
        details = seg.get("details") or ""
        score = 0.0

        wanted_colors = [keys for word, keys in COLOR_WORDS.items() if word in text]
        if wanted_colors:
            matched = any(k in subject for keys in wanted_colors for k in keys)
            score += 0.60 if matched else -1.20

        for text_keys, act_keys in ACTION_RULES:
            if not any(k in text for k in text_keys):
                continue
            if any(k in action or k in details for k in act_keys):
                score += 0.45
            else:
                score -= 0.25

        hits = sum(1 for term in DETAIL_TERMS if term in text and term in details)
        score += min(0.30, 0.15 * hits)
        return score

    def pick(self, hint: str | None, need: float, *, line_text: str = "",
             subject_lock: str | None = None) -> dict | None:
        # 硬约束：最近 3 个镜头用过的素材不再选，避免同机位连续出现
        recent = set(self.history[-3:])
        pool = [s for s in self.segments
                if s["clipId"] not in recent
                and self.usage.get(s["clipId"], 0) < self.MAX_CLIP_REUSE]
        if not pool:
            pool = [s for s in self.segments if s["clipId"] not in recent] or self.segments

        best = None
        best_score = -1e9
        for seg in pool:
            if (seg["clipId"], seg["start"]) in self.used:
                continue
            avail = seg["end"] - seg["start"]
            if avail < 0.35:
                continue

            score = seg["score"]
            if hint and hint in seg["kinds"]:
                score += 0.45
            if hint and seg["kinds"] and hint not in seg["kinds"]:
                score -= 0.3
            # 优先能一次覆盖所需时长的片段，减少镜头切换
            if avail >= need:
                score += 0.5
            else:
                score -= 0.25 * (need - avail)

            if seg["clipId"] in self.history[-4:]:
                score -= 0.7
            if seg["clipId"] in self.history[-8:]:
                score -= 0.25
            # 静态图信息量低，只在没有更合适的动态素材时才用
            if seg.get("static"):
                score -= 0.35

            # 画面新颖度：和已选镜头越像，扣分越重
            score -= self._novelty_penalty(seg)
            # 语义对齐：这句话讲的卖点，画面里有没有对应证据
            score += self._semantic_score(line_text, seg)
            # 主体连贯：同一句拆成多个镜头时，必须还是同一个商品
            if subject_lock:
                same = subject_lock in (seg.get("subject") or "")
                score += self.SUBJECT_BONUS if same else -1.2

            if score > best_score:
                best, best_score = seg, score

        if best is None:
            # 全部用过一轮：清空记录重来，保证一定能出片
            if self.exhausted_once:
                return None
            self.exhausted_once = True
            self.used.clear()
            self.usage.clear()
            return self.pick(hint, need)

        self.used.add((best["clipId"], best["start"]))
        self.history.append(best["clipId"])
        self.usage[best["clipId"]] = self.usage.get(best["clipId"], 0) + 1
        sig = self._sig(best)
        if sig is not None:
            self.used_sigs.append(sig)
        return best

    @staticmethod
    def available(seg: dict) -> float:
        """片段可用长度：候选窗口 + 允许的一小段收手。"""
        window = seg["end"] - seg["start"]
        room = max(0.0, seg["clipDuration"] - seg["end"])
        return window + min(MAX_TAIL_EXTEND, room)


def tail_motion(path: Path, out_point: float, frames_dir: Path, tag: str) -> float:
    """测量出点附近的画面运动量，用来判断该用硬切还是溶解。"""
    try:
        a = ffmpeg.frame_at(path, max(0.0, out_point - 0.22),
                            frames_dir / f"{tag}_a.jpg", width=240)
        b = ffmpeg.frame_at(path, max(0.02, out_point - 0.04),
                            frames_dir / f"{tag}_b.jpg", width=240)
        return metrics.diff(metrics.load_rgb(a), metrics.load_rgb(b))
    except Exception:  # noqa: BLE001
        return 1.0  # 测不出来时保守地用溶解


def build(style: dict, index: dict, script: dict, tts: dict, *, height: int, fps: int,
          gap: float, canvas_mode: str = "source",
          signatures: dict[str, list[float]] | None = None) -> dict:
    target_w, target_h = canvas_for(canvas_mode, style, index, height)

    picker = Picker(index, signatures)
    frames_dir = project.WORK / "plan" / "focus"
    frames_dir.mkdir(parents=True, exist_ok=True)

    shots: list[dict] = []
    lines: list[dict] = []

    for i, line in enumerate(script["lines"]):
        tts_line = tts["lines"][i]
        speak = float(tts_line["duration"])
        need = round(speak + gap, 3)
        hint = line.get("shotHint")
        remaining = need
        line_shots: list[dict] = []
        subject_lock: str | None = None

        while remaining > 0.02:
            seg = picker.pick(hint, remaining, line_text=tts_line["text"],
                              subject_lock=subject_lock)
            if seg is None:
                raise SystemExit(
                    f"第 {i + 1} 句还需要 {remaining:.2f}s，但素材已用尽。"
                    f"请补充素材或缩短文案：{tts_line['text']}"
                )

            avail = Picker.available(seg)
            take = min(remaining, avail)
            # 量化到整帧：否则 xfade 的 offset 会和实际镜头长度差半帧，转场直接越界截断
            take_frames = max(1, int(round(take * fps)))
            take = round(take_frames / fps, 4)
            if take < MIN_SHOT_SEC and remaining > MIN_SHOT_SEC:
                # 这个片段太短，跳过换下一个
                continue

            src_in = float(seg["start"])
            src_out = round(src_in + take, 3)
            if src_out > seg["clipDuration"]:
                src_in = round(max(0.0, seg["clipDuration"] - take), 3)
                src_out = round(src_in + take, 3)

            geo = crop_geometry(seg["srcW"], seg["srcH"], target_w, target_h)
            focus_x = None
            if geo["w"] < seg["srcW"] and not seg.get("static"):
                probe_t = min(max(0.0, seg["clipDuration"] - 0.05), src_in + 0.25)
                frame = ffmpeg.frame_at(project.ROOT / seg["path"], probe_t,
                                        frames_dir / f"focus_{len(shots) + 1:03d}.jpg", width=480)
                arr = metrics.load_rgb(frame)
                scale = arr.shape[1] / seg["srcW"]
                x_small = metrics.best_crop_x(arr, max(2, int(round(geo["w"] * scale))))
                focus_x = int(round(x_small / scale))
                geo = crop_geometry(seg["srcW"], seg["srcH"], target_w, target_h, focus_x=focus_x)

            shot = {
                "index": len(shots) + 1,
                "lineIndex": i + 1,
                "caption": tts_line["text"],
                "intent": line.get("intent"),
                "source": seg["path"],
                "clipId": seg["clipId"],
                "clipDuration": seg["clipDuration"],
                "sourceIn": round(src_in, 3),
                "sourceOut": src_out,
                "duration": take,
                "frames": take_frames,
                "speed": 1.0,
                "crop": geo,
                "focusX": focus_x,
                "grade": DEFAULT_GRADE,
                "extendedIntoMotion": round(max(0.0, take - (seg["end"] - seg["start"])), 3),
                "segmentScore": seg.get("score"),
                "segmentKinds": seg.get("kinds"),
                "subject": seg.get("subject", ""),
                "action": seg.get("action", ""),
                "details": seg.get("details", ""),
                "semanticScore": round(picker._semantic_score(tts_line["text"], seg), 3),
                "static": bool(seg.get("static")),
                "thumbnailHint": seg.get("description"),
            }
            shots.append(shot)
            line_shots.append(shot)
            if subject_lock is None:
                subject_lock = seg.get("subject") or None
            remaining = round(remaining - take, 3)

    # ---- 合并过短的收尾镜头：拆句剩下的零点几秒不该独立成镜
    merged: list[dict] = []
    for shot in shots:
        if (merged and shot["duration"] < MIN_SHOT_SEC
                and merged[-1]["lineIndex"] == shot["lineIndex"]):
            prev = merged[-1]
            prev["duration"] = round(prev["duration"] + shot["duration"], 4)
            prev["frames"] += shot["frames"]
            prev["sourceOut"] = round(prev["sourceOut"] + shot["duration"], 4)
            continue
        merged.append(shot)
    shots = merged
    for i, shot in enumerate(shots, 1):
        shot["index"] = i

    # ---- 句子由镜头反推，保证拆分/合并后仍然一致
    lines = []
    for i, raw_line in enumerate(script["lines"]):
        tts_line = tts["lines"][i]
        owned = [s for s in shots if s["lineIndex"] == i + 1]
        if not owned:
            raise SystemExit(f"第 {i + 1} 句没有分配到任何镜头：{tts_line['text']}")
        lines.append({
            "index": i + 1,
            "text": tts_line["text"],
            "tts": tts_line["file"],
            "audioDuration": float(tts_line["duration"]),
            "duration": round(sum(s["duration"] for s in owned), 4),
            "shotIndexes": [s["index"] for s in owned],
        })

    # ---- 决定每个衔接处用硬切还是溶解
    # 判据只看"出点这一帧画面是否在动"：静止帧硬切最隐形；
    # 手正在收商品时画面在动，硬切会跳，改用短溶解接走。
    for idx, shot in enumerate(shots):
        is_last = idx == len(shots) - 1
        if is_last:
            shot["transitionOut"] = {"type": "none", "duration": 0.0}
            shot["tailMotion"] = 0.0
            continue
        motion = tail_motion(project.ROOT / shot["source"], shot["sourceOut"],
                             frames_dir, f"tail_{shot['index']:03d}")
        shot["tailMotion"] = round(motion, 4)
        dissolves = motion > MOTION_CUT_THRESHOLD
        shot["transitionOut"] = {
            "type": "xfade" if dissolves else "cut",
            "duration": DISSOLVE_SEC if dissolves else round(1.0 / fps, 3),
        }

    # ---- 为转场预留尾部素材：溶解借用镜头后面多出来的那几帧，不压缩时间轴
    # 所有长度都量化到整帧，保证 xfade 的 offset 与实际镜头长度精确对齐
    for idx, shot in enumerate(shots):
        junction = shot["transitionOut"]
        if junction["type"] == "none":
            shot["renderFrames"] = shot["frames"]
            shot["renderDuration"] = round(shot["frames"] / fps, 4)
            continue

        want_frames = max(1, int(round(junction["duration"] * fps)))
        room_frames = int((shot["clipDuration"] - shot["sourceOut"]) * fps + 1e-6)
        if room_frames < want_frames:
            # 尾部可借的素材不够，退化为 1 帧硬切（尾部用 tpad 补齐，不会缺帧）
            junction["type"] = "cut"
            want_frames = 1
        junction["duration"] = round(want_frames / fps, 4)
        shot["renderFrames"] = shot["frames"] + want_frames
        shot["renderDuration"] = round(shot["renderFrames"] / fps, 4)
        shot["sourceOut"] = round(shot["sourceOut"] + want_frames / fps, 4)

    # ---- 时间轴：每句按自身时长推进，转场不再压缩总时长
    cursor = 0.0
    line_starts: dict[int, float] = {}
    for shot in shots:
        shot["start"] = round(cursor, 3)
        shot["end"] = round(cursor + shot["duration"], 3)
        cursor = round(cursor + shot["duration"], 4)
        line_starts.setdefault(shot["lineIndex"], shot["start"])

    total = round(cursor, 3)

    # ---- 字幕按句子走，不按镜头走；相邻句子首尾相接，避免叠字
    events = []
    for idx, line in enumerate(lines):
        start = line_starts[line["index"]]
        end = line_starts[lines[idx + 1]["index"]] if idx + 1 < len(lines) else total
        events.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "text": line["text"],
            "lineIndex": line["index"],
        })

    captions = {
        "style": {
            "fontFamily": script.get("caption", {}).get("fontFamily", "PingFang SC"),
            "fontSize": int(round(target_h * 0.030)),
            "primaryColor": "&H00FFFFFF",
            "outlineColor": "&H00000000",
            "outlineWidth": max(1, round(target_h * 0.0022)),
            "shadow": 0,
            "marginV": int(round(target_h * (1 - (style["captions"]["band"]["y"]
                                                  + style["captions"]["band"]["height"])))),
            "alignment": 2,
        },
        "disclaimer": style.get("disclaimer"),
        "events": events,
    }

    return {
        "schemaVersion": 1,
        "canvas": {"width": target_w, "height": target_h, "fps": fps, "mode": canvas_mode},
        "audio": {
            "voice": tts.get("voice"),
            "speed": tts.get("speed"),
            "targetLufs": -14.0,
            "truePeakDb": -1.5,
            "bgm": script.get("bgm"),
            "lines": [
                {"start": line_starts[line["index"]], "file": line["tts"],
                 "duration": line["audioDuration"]}
                for line in lines
            ],
        },
        "captions": captions,
        "shots": shots,
        "lines": lines,
        "totalDuration": total,
        "styleRef": str((project.WORK / "reference" / "style-spec.json").relative_to(project.ROOT)),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--style", default=str(project.WORK / "reference" / "style-spec.json"))
    ap.add_argument("--material", default=str(project.WORK / "material" / "material-index.json"))
    ap.add_argument("--script", default=str(project.WORK / "script.json"))
    ap.add_argument("--tts", default=str(project.WORK / "tts" / "manifest.json"))
    ap.add_argument("--out", default=str(project.WORK / "edit-plan.json"))
    ap.add_argument("--canvas", default=project.DEFAULT_CANVAS,
                    help="source=跟随素材比例不裁切；reference=跟随参考片；也可写 9:16 / 3:4")
    ap.add_argument("--height", type=int, default=project.DEFAULT_HEIGHT,
                    help="输出画幅高度，宽度按画布比例推导")
    ap.add_argument("--fps", type=int, default=project.DEFAULT_FPS)
    ap.add_argument("--gap", type=float, default=0.12, help="每句之间的留白（秒）")
    args = ap.parse_args()

    with Step("plan") as s:
        style = read_json(Path(args.style))
        index = read_json(Path(args.material))
        script = read_json(Path(args.script))
        tts = read_json(Path(args.tts))

        if len(script["lines"]) != len(tts["lines"]):
            raise SystemExit(
                f"文案句数（{len(script['lines'])}）与配音句数（{len(tts['lines'])}）不一致，请重跑 tts.py"
            )

        sig_path = Path(args.material).with_name("signatures.json")
        signatures = read_json(sig_path) if sig_path.exists() else None
        if signatures is None:
            log(s.name, "缺少 signatures.json，本轮退化为按分数选镜（建议重跑 analyze_material.py）",
                level="warn")

        plan = build(style, index, script, tts, height=args.height, fps=args.fps,
                     gap=args.gap, canvas_mode=args.canvas, signatures=signatures)
        write_json(Path(args.out), plan)

        dissolves = sum(1 for s in plan["shots"] if s["transitionOut"]["type"] == "xfade")
        log(s.name, f"{len(plan['shots'])} 个镜头 / {len(plan['lines'])} 句，"
                    f"总长 {plan['totalDuration']:.1f}s，"
                    f"转场 {dissolves} 处，画布 {plan['canvas']['width']}x{plan['canvas']['height']}")


if __name__ == "__main__":
    main()
