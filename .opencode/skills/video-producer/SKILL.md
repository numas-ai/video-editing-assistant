---
name: video-producer
description: 抖音带货短视频生产线。给定一批原始素材与一条参考成品，拆解参考片的节奏/文案/字幕/色调，生成同风格的原创成片（本地 TTS 配音 + FFmpeg 渲染 + 自动质检）。当用户要求"按某条视频的风格剪一条"、"把这批素材做成抖音带货片"、"复刻这个视频的节奏"时使用。
license: MIT
compatibility: opencode
metadata:
  audience: video-editors
  domain: short-video
  opencode/slash: "true"
---

# 视频流水线生产线

把"参考成品 → 风格规格 → 原创成片"这条链路固化成可复用的生产线。
换素材、换参考片都能跑，不绑定任何一条具体视频。

## 核心约定

- **风格是数据**：参考片拆解结果落在 `work/reference/style-spec.json`，不写死在代码里。
- **结构是数据**：成片时间线落在 `work/edit-plan.json`，可 diff、可手改、可局部重跑。
- **Agent 只做决策**：写文案、挑风格取向、判断取舍；渲染与合成一律交给脚本。
- **每阶段可单独重跑**，产物都在 `work/` 下按阶段分目录。

## 目录

```
.opencode/skills/video-producer/
├── SKILL.md              本文件
├── README.md             给人看的部署与用法
├── lib/                  公共库（路径、ffmpeg、视觉模型、指标）
├── scripts/              各阶段脚本
├── schemas/              JSON 契约
└── reference/            抖音带货片方法论
```

项目侧目录：

```
学习素材/     原始素材（只读）
参考视频/     参考成品 + 抓取元信息
work/         全部中间产物
output/       成片
.tools/       依赖与模型（不入库）
```

## 开工前必读

动任何一步之前先读这三份，它们决定"做什么"和"什么算做完"：

| 文件 | 作用 |
|---|---|
| `reference/quality-bar.md` | 交付判定标准，`qc.py` 按它打分 |
| `reference/lessons.md` | 历次验收被否掉的原因，避免重犯 |
| `templates/README.md` + `templates/script.example.json` | 文案结构和硬约束 |

## 执行顺序

先自检，再按 0→8 走。每一步失败只重跑那一步，不要从头来。

```bash
PY=.tools/py/pipeline/bin/python
S=.opencode/skills/video-producer/scripts

# 0. 环境（缺依赖时先跑这条，它会装到 doctor 全绿）
bash $S/bootstrap.sh            # 加 --no-tts 跳过 6GB 的本地配音
bash $S/bootstrap.sh --dry-run  # 只看会装什么，不动系统
```

`bootstrap.sh` 负责：Homebrew → ffmpeg-full / uv / node → Ollama + 视觉模型 →
Python/Node 依赖 → 本地 TTS → `doctor.py` 全绿才退出 0。

已经装好的环境直接跑自检：

```bash
$PY $S/doctor.py
```

### 1. 取参考片（可选，已有文件则跳过）

```bash
node $S/fetch_reference.mjs --url "<分享链接|视频ID>" --out 参考视频 --name reference
```

产出 `参考视频/reference.mp4` + `.json`（来源、时长、校验和）。
被风控拦时加 `--headed`，人工过验证码后再跑。

### 2. 拆解参考片

```bash
$PY $S/analyze_reference.py
```

产出 `work/reference/style-spec.json`：
画幅、镜头数、镜长分布、每个镜头的描述与构图类型、字幕带位置、逐句文案与时间、响度/节拍、免责声明角标。

要点：文案用**本地视觉模型读字幕带**，不是 OCR 猜字；因此不联网也不再依赖 tesseract。

### 3. 素材体检

```bash
$PY $S/analyze_material.py --input 学习素材
```

产出 `work/material/material-index.json`：每段素材的构图类型、内容描述，以及若干**可直接使用的候选片段**（含清晰度/抖动/曝光打分）。

### 4. 写文案

手工或由 Agent 生成 `work/script.json`：

```json
{
  "voice": { "spkId": "default", "speed": 1.3 },
  "bgm": { "file": "assets/bgm/xxx.mp3", "gainDb": -20 },
  "lines": [
    { "id": 1, "text": "挖到宝了", "intent": "hook", "shotHint": "flatlay" }
  ]
}
```

- `intent` 只是标注，用于复盘；`shotHint` 会影响挑镜头（`flatlay` / `handheld` / `product_closeup` / `model_wear` / `package`）。
- 文案必须能由**手上真实素材**支撑，不要写素材里看不到的效果。
- 参考 `reference/douyin-benchmark.md` 的叙事骨架。

### 画面与口播的对齐（自动）

选镜不是"挑高分画面"，而是让画面服从口播：

1. `analyze_material.py` 会给每个候选片段打「主体｜动作」标注（例如「棕色三角杯内衣｜手托正面展示」）。
2. `plan_edit.py` 用 `SEMANTIC_RULES` 把文案里的卖点词和画面证据对上：
   文案提到"前扣"，画面标注里必须出现"前扣/扣/搭扣"，命中加分，提了却完全不沾则扣分。
3. 一句话被拆成多个镜头时，**锁死主体**：第一镜选了"棕色三角杯"，后续镜头只能继续用同主体。
4. 每个镜头的语义命中值写进 `edit-plan.json` 的 `semanticScore`，并在 `work/review/shot-table.md`
   里逐句列出，供验收时核对"说的是不是这个"。

调语义映射：改 `plan_edit.py` 的 `SEMANTIC_RULES`（左边是文案说法，右边是画面证据）。

### 5. 配音

```bash
$PY $S/tts.py
```

产出 `work/tts/line_XXX.wav` + `manifest.json`（真实时长，编排依赖它）。

### 6. 编排 + 渲染

```bash
$PY $S/plan_edit.py --height 1440
$PY $S/render.py --name final
```

`plan_edit.py` 会按配音时长挑镜头、做**主体感知裁切**（避免 4:3→3:4 把产品裁掉）、
对齐字幕带位置；`render.py` 逐镜头归一化 → 硬切拼接 → 配音落位 + BGM 闪避 + 响度标准化 → 烧字幕封装。

### 7. 质检

```bash
$PY $S/qc.py
```

检查规格、响度、黑帧、静音、音画时长偏差，输出 `work/qc/qc-report.json` 与缩略图。
有阻断项时退出码非 0。

### 8. 生成审阅证据

```bash
$PY $S/review.py --input output/预览.mp4
```

产出 `work/review/`：逐镜头缩略图、转场三帧对照、镜头表。用于下一步给用户看。

### 9. 版本存档（不可跳过）

```bash
$PY $S/version.py save --note "这一版改了什么"
```

每次渲染后必须存档，生成只读的 `output/vNN/`。用户随时可能说"还是第 N 版好"，
没有存档就只能重做。`--note` 要写成用户能看懂的一句话。

用户要回到某一版：

```bash
$PY $S/version.py restore v03   # 以 v03 为基础继续改（产出新版本，不覆盖 v03）
$PY $S/version.py list          # 列出所有版本
$PY $S/version.py diff v01 v03  # 对比两版差异
```

---

## 验收与迭代闭环（强制）

**渲染完成不等于交付完成。** 只有用户明确认可，才算交付。每次出片都必须走完三道闸门。

### 闸门 1：机器自检（不可跳过）

跑 `qc.py`。它会同时对照 `reference/quality-bar.md` 打分，输出三类指标：

- **结构**：镜头数、平均镜长、P10/P90、溶解占比
- **语义**：命中率、相似度 P90、同素材复用次数
- **红线**：响度、真峰值、黑帧、时长偏差

`verdict = fail` 时必须修完再继续；`warn` 允许交付，但必须在验收时向用户说明是哪一项没达标。

按 `issue.code` 自动纠正，改完重跑，直到只剩 `warn`：

| code | 自动动作 |
|---|---|
| `LUFS_OUT_OF_RANGE` | 调 `edit-plan.json` 的 `audio.targetLufs`，重跑 `render.py` |
| `TRUE_PEAK_TOO_HIGH` | 把 `audio.truePeakDb` 降到 -1.5，重跑 `render.py` |
| `BLACK_FRAMES` | 定位该时间点所属镜头，修正其 `sourceIn/sourceOut`，重跑 `plan_edit.py` + `render.py` |
| `CAPTION_VOICE_MISMATCH` | `script.json` 句数与配音不一致，重跑 `tts.py` → `plan_edit.py` → `render.py` |
| `DURATION_DRIFT` | 转场链越界或帧对齐漂移，重跑 `plan_edit.py` → `render.py` |
| `SILENCE` | 未挂 BGM 时属正常；已挂 BGM 仍静音则查 `bgm.file` / `gainDb` |

### 闸门 2：证据自检（不可跳过）

跑 `review.py`，然后**逐项看图自检**，不要只看数字：

1. 缩略图里有没有"商品被裁掉 / 主体跑到画面边缘"的镜头
2. 有没有连续两个镜头是同一机位、同一角度（观众会觉得卡住）
3. 转场对照里，溶解有没有糊成一团或闪白
4. 有没有镜头在"手把商品收走"的过程中停留超过 0.5 秒
5. 字幕有没有压到商品、有没有超出安全区、有没有断句成半个词
6. 镜头与文案是否对得上（讲到"前扣"时画面是不是前扣）

发现问题先自己修，**不要带着已知问题去问用户**。

### 闸门 3：用户验收（必须走 `question`）

把证据和结论一起交给用户：成片路径、时长、响度、镜头数、转场数、审阅图路径，然后提问。

- 用户认可 → 交付结束
- 用户指出问题 → 按下表定位到阶段，只重跑受影响的部分，回到闸门 1

| 用户反馈 | 只重跑 |
|---|---|
| 文案不准确 / 卖点不对 | `script.json` → `tts.py` → `plan_edit.py` → `render.py` |
| 某个镜头选得不好 | 直接在 `edit-plan.json` 里改该镜头的 `clipId/sourceIn/sourceOut` → `render.py` |
| 节奏太快/太慢 | `plan_edit.py --gap` 或 `script.json` 的 `voice.speed` |
| 收手画面停留太久 / 转场生硬 | 调 `plan_edit.py` 的 `MAX_TAIL_EXTEND`、`DISSOLVE_SEC`、`MOTION_CUT_THRESHOLD` |
| 字幕大小/位置/字体 | `plan_edit.py` 的字幕比例 + `script.json` 的 `caption` |
| 调色不对 | `plan_edit.py` 的 `DEFAULT_GRADE` |
| 画面比例/裁切 | `plan_edit.py --canvas / --height` |

### 必须停下来问用户的情况

以下判断不得自行拍板，一律用 `question`（一次只问一个，写清是什么/为什么/建议/影响）：

- 文案主张超出素材能证明的范围（例如没有真人镜头却写"上身效果"）
- 节奏、画面观感、字幕位置这类主观偏好
- 涉及版权：BGM、字体、肖像、平台规则
- 素材不足，需要补拍或换叙事角度
- 同一问题已改 2 轮仍未达标——说明方向可能错了，先对齐目标再动手

### 验收后必须留痕

用户认可（或否掉）之后，把这一轮结论追加到 `reference/lessons.md`：

- 被否掉的是什么**现象**（要具体到第几秒/第几句）
- **根因**落在哪个阶段（文案 / 选镜 / 转场 / 渲染 / 环境）
- 改了**哪个参数**或哪句文案

下次开工前先读它。这是这条流水线唯一的"学习"机制——
没有自动进化，靠的是把每次人工反馈沉淀成下一次的先验。

### 自检清单（交付前逐条确认）

- [ ] `qc.py` 无阻断项，且 `quality.checks` 全部达标或已向用户解释
- [ ] `review.py` 的证据图已逐张看过
- [ ] 没有镜头在收手动作上停留超过 0.5 秒
- [ ] 相邻镜头没有同机位重复
- [ ] 字幕与文案、音频三者对齐
- [ ] 有画面诉求的句子命中率 ≥ 60%
- [ ] 用户已用 `question` 明确认可
- [ ] 已用 `version.py save` 存档，`成片/版本清单.md` 有这一版
- [ ] 本轮结论已追加到 `reference/lessons.md`

---

## 宿主兼容与降级

本技能依赖三样宿主能力。缺了必须显式降级并告知用户，**不允许静默降级**。

| 能力 | 具备时 | 不具备时 |
|---|---|---|
| `question` 工具 | 一次一问，用户点选 | 用纯文本提问，但仍遵守「一次只问一个问题 + 是什么/为什么/建议/影响」；不能因为没工具就跳过确认 |
| `skill` 自动加载 | 直接进流程 | 让用户把本文件内容贴进上下文，按章节手动执行 |
| 本地 Ollama | 语义标注可用，画面服从口播 | 先跑 `bootstrap.sh`；确实装不了就用 `--skip-vlm`，并在交付时明确声明"未做语义对齐，画面按分数选" |

跨宿主使用时，`question` 工具不可用不影响主链路：抓参考片、拆解、编排、渲染、质检都不依赖它。


## 数据契约

见 `schemas/`。改动任一契约需同步更新 schema 与下游脚本，并在 PR 说明里标注不兼容点。

## 调参入口

| 想要 | 改哪里 |
|---|---|
| 画幅策略 | `plan_edit.py --canvas source`（跟随素材，不裁切，默认）/ `reference`（跟随参考片，会裁切） |
| 画幅高度/帧率 | `plan_edit.py --height / --fps` |
| 每句留白 | `plan_edit.py --gap` |
| 语速 | `script.json` → `voice.speed` |
| 字幕字体/字号比例 | `script.json` → `caption.fontFamily`；字号在 `plan_edit.py` 按画高比例推导 |
| 调色 | `plan_edit.py` → `DEFAULT_GRADE` |
| 候选片段时长 | `analyze_material.py --window / --hop / --per-clip` |
| 镜头切换敏感度 | `analyze_reference.py --scene-threshold` |
| 文案与画面的语义映射 | `plan_edit.py` → `SEMANTIC_RULES` |
| 画面去重强度 | `plan_edit.py` → `DIVERSITY_WEIGHT` / `SIM_BASELINE` |
| 收手段落长度与转场 | `plan_edit.py` → `MAX_TAIL_EXTEND` / `DISSOLVE_SEC` / `MOTION_CUT_THRESHOLD` |

## 质量红线

- 文案不得主张素材中不存在的效果（例如没有真人镜头就不要写"上身效果"）。
- 用了 AI 配音就必须在片中标注（抖音规则）；`style-spec.json` 里检测到的角标会自动写进字幕。
- 响度落在 -16 ~ -12 LUFS，真峰值不超过 -0.5 dBTP。
- 字幕不得超出安全区，单行不超过参考片的最大字数。

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| `ass` 滤镜不存在 | 用的是精简版 ffmpeg | `brew install ffmpeg-full`，脚本会自动优先用它 |
| 字幕里中文变方块 | libass 找不到字体 | `script.json` 指定已安装字体，或装字体后 `fc-cache -f` |
| 视觉模型超时 | Ollama 没起/模型未拉 | `ollama serve`、`ollama pull qwen3-vl:4b-instruct` |
| 抓参考片为空 | 风控验证 | `--headed` 人工过验证 |
| 镜头裁掉了产品 | 主体偏一侧 | 调 `metrics.best_crop_x` 的偏移上限，或在该镜头手写 `crop.x` |
| 配音太慢/太快 | 语速不合适 | 调 `voice.speed`，重新跑第 5 步起 |
