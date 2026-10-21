# 抖音带货视频生产线 · 使用手册

把「一批原始素材」+「一条参考成品」变成「一条同风格的原创成片」。
全流程本机运行，素材不出本机。

---

## 0. 一句话说清它是什么

你给它两样东西：

1. **素材**：自己拍的原始视频（横的竖的都行，一段段的）
2. **参考片**：一条你觉得"效果对了"的抖音成品

它给你一条新片子：**结构、节奏、画幅、字幕位置照着参考片来，文案和画面重新生成**，配音是本机合成的，不抄原片内容。

---

## 1. 30 秒上手

```bash
# 1) 装环境（第一台机器只跑一次，约 30–50 分钟，主要在下模型）
bash .opencode/skills/video-producer/scripts/bootstrap.sh

# 2) 确认环境没问题
.tools/py/pipeline/bin/python .opencode/skills/video-producer/scripts/doctor.py

# 3) 把素材丢进 学习素材/，参考片丢进 参考视频/reference.mp4
#    然后写文案：复制模板改一改
cp .opencode/skills/video-producer/templates/script.example.json work/script.json

# 4) 一键跑完
bash .opencode/skills/video-producer/scripts/run_pipeline.sh "" 成片名
```

成片在 `output/成片名.mp4`。

> 第一次跑之前，先把 `work/script.json` 里的 26 句改成你产品的内容。
> 不改也能跑，但出来的片子讲的是内衣。

---

## 2. 它具备什么能力

### 能做的

| 能力 | 说明 |
|---|---|
| **拆解参考片** | 自动识别镜头切换点、每个镜头的构图类型、字幕位置与逐句文案、音轨节奏与响度 |
| **素材体检** | 把每段素材切成可用片段，按清晰度/抖动/曝光打分，并标注「主体｜动作｜细节」 |
| **按口播选画面** | 文案说"前扣"，画面就找有前扣证据的镜头；颜色/花纹必须落在商品主体上，不被背景干扰 |
| **文案与画面互锁** | 同一句拆成多个镜头时锁定同一主体；素材里没有的画面会提醒你改文案 |
| **本地配音** | CosyVoice3 端侧合成，26 句约 2.5 分钟，支持固定音色，后续可换克隆音色 |
| **声明式时间线** | 所有剪辑决策落在 `edit-plan.json`，可读、可改、可 diff，改一个镜头不用重跑全片 |
| **自然转场** | 出点落在运动中（手在收商品）用短溶解，静止帧用硬切；不会硬切在动作中间 |
| **画面去重** | 用画面指纹判断"是不是同一件事"，同素材最多复用 2 次 |
| **字幕烧录** | ASS 字幕，字号/位置按参考片实测比例推导，白字黑边 |
| **自动质检** | 响度、真峰值、黑帧、音画偏差、镜长分布、语义命中率、画面相似度，对照基线打分 |
| **审阅证据** | 自动生成逐镜头缩略图、转场三帧对照、逐句对齐表，方便验收 |
| **自我纠错** | 质检问题带 `fix` / `rerun` 字段，能定位到阶段并只重跑那一部分 |

### 明确不做的

| 不做 | 原因 |
|---|---|
| **不认识没见过的东西** | 它靠视觉模型描述画面，模型认不出的商品/动作，匹配会失准 |
| **不替你想卖点** | 文案要人写（或由 AI 写），它只负责"让画面服从文案" |
| **不会凭空造画面** | 素材里没有真人上身，就配不出上身效果，只能改文案 |
| **不处理版权** | BGM、字体、肖像权要你自己确认，它只负责混音和烧录 |
| **不是剪辑软件** | 没有时间线拖拽界面，改片子靠改 JSON 或改文案 |

---

## 3. 工作原理

```
参考视频/reference.mp4
        │
        ▼
  ① 拆解参考片 ──────────► work/reference/style-spec.json
        │                  镜头表 / 字幕带 / 逐句文案 / 节奏 / 响度
        ▼
  学习素材/*.MOV
        │
        ▼
  ② 素材体检 ────────────► work/material/material-index.json
        │                  候选片段 + 质量分 + 主体/动作/细节标注
        │                  + signatures.json（画面指纹，用于去重）
        ▼
  work/script.json（人写）
        │
        ▼
  ③ 配音 ────────────────► work/tts/*.wav + manifest.json（真实时长）
        │
        ▼
  ④ 编排 ────────────────► work/edit-plan.json
        │                  每句配哪个镜头、入出点、转场、字幕
        ▼
  ⑤ 渲染 ────────────────► work/render/*.mp4
        │                  逐镜头归一化 → 转场拼接 → 混音 → 烧字幕
        ▼
  ⑥ 质检 ────────────────► work/qc/qc-report.json（对照质量基线）
        │
        ▼
  ⑦ 审阅 ────────────────► work/review/（缩略图 / 转场对照 / 对齐表）
        │
        ▼
   用户验收（question）
```

### 每个阶段在干什么

| 阶段 | 输入 | 输出 | 首次耗时 | 重跑耗时 |
|---|---|---|---|---|
| ① 拆解参考片 | 参考片 | 风格规格 | 5–10 分钟 | < 1 秒（有缓存） |
| ② 素材体检 | 素材目录 | 素材索引 + 指纹 | 10 分钟 / 40 段 | 1 分钟（有缓存） |
| ③ 配音 | 文案 | 逐句 wav | 2.5 分钟 / 26 句 | 2.5 分钟 |
| ④ 编排 | 文案+素材+风格 | 时间线 JSON | 10 秒 | 10 秒 |
| ⑤ 渲染 | 时间线 | 成片 | 35 秒 | 35 秒 |
| ⑥ 质检 | 成片+时间线 | 报告 | 5 秒 | 5 秒 |
| ⑦ 审阅 | 成片+时间线 | 证据图 | 15 秒 | 15 秒 |

> 耗时基于 Apple M5 / 32GB 实测。视觉模型在 CPU 上跑，换更弱的机器会明显变慢。

---

## 4. 第一次使用：从零到第一条成片

### Step 0 · 装环境

```bash
bash .opencode/skills/video-producer/scripts/bootstrap.sh
```

它会依次安装并检查：

| 组件 | 用途 | 体积 |
|---|---|---|
| Homebrew | 包管理 | — |
| ffmpeg-full | 剪辑引擎（必须带 libass，否则字幕烧不上去） | ~60MB |
| uv + Python 3.11 | 分析/编排/渲染的运行环境 | ~100MB |
| Node + Playwright | 抓取抖音参考片 | ~200MB |
| Ollama + qwen3-vl:4b-instruct | 读字幕、给画面打标签 | ~3.3GB |
| CosyVoice3 + PyTorch | 本地配音 | ~6GB |

装完约 **11GB**，跑完 `doctor.py` 全绿才算成功。

- 不想装 6GB 的配音：`bash .../bootstrap.sh --no-tts`
- 只想看会装什么：`bash .../bootstrap.sh --dry-run`

### Step 1 · 放素材

把你的原始视频放进 `学习素材/`。

- 支持 `.mov .mp4 .m4v .avi .mkv .webm` 和 `.heic .jpg .png`
- 不用改名、不用排序、不用剪
- 手机竖拍（带旋转元数据）会自动识别，不会拍成横的

### Step 2 · 拿参考片

**方式 A：已有文件**（推荐）

```bash
cp 你的参考片.mp4 参考视频/reference.mp4
```

**方式 B：从抖音链接抓**

```bash
node .opencode/skills/video-producer/scripts/fetch_reference.mjs \
     --url "https://v.douyin.com/xxxx/" --out 参考视频 --name reference
```

抓不到时加 `--headed`，会弹出真浏览器，你手动过一次验证码即可。

### Step 3 · 拆解参考片

```bash
.tools/py/pipeline/bin/python .opencode/skills/video-producer/scripts/analyze_reference.py
```

产出 `work/reference/style-spec.json`，里面是这条片子的"风格坐标"：

- 画幅比例、总时长、镜头数、平均镜长
- 每个镜头的起止、构图类型、画面描述
- 字幕带的位置和高度（用像素比例而不是绝对像素）
- 逐句文案和出现时间
- 响度、节拍、有没有人声/配乐

### Step 4 · 素材体检

```bash
.tools/py/pipeline/bin/python .opencode/skills/video-producer/scripts/analyze_material.py
```

产出 `work/material/`：

- `material-index.json`：每段素材的可用片段、质量分、「主体｜动作｜细节」标注
- `signatures.json`：每段的画面指纹，用于去重

> 如果视觉模型没起来，这一步会直接失败并告诉你怎么修，**不会静默降级**。

### Step 5 · 写文案（最关键的一步）

```bash
cp .opencode/skills/video-producer/templates/script.example.json work/script.json
```

然后改 `lines`。硬约束：

| 项 | 要求 |
|---|---|
| 句数 | 22–30 句 |
| 单句字数 | 6–12 字 |
| 总时长 | 40–55 秒 |
| 钩子 | 前 3 句内出现 |
| 语速 | `voice.speed` 1.2–1.4 |

**最重要的一条：每写一句，问自己"这句话里的哪个词，画面必须给出证据"。**
拿这个词去 `work/material/material-index.json` 里搜，搜不到就改写。

```
✓ 能写：前扣、肩带、底围、杯垫、浅蓝边、棕色、黑色、豹纹、多款、包装袋、走线
✗ 别写：上身显瘦、聚拢、透气、无痕、用了三个月不变形（素材证明不了）
```

### Step 6 · 配音

```bash
.tools/py/pipeline/bin/python .opencode/skills/video-producer/scripts/tts.py
```

产出 `work/tts/`：逐句 wav + `manifest.json`（含每句真实时长）。

首次会加载模型约 15 秒，之后每句 3–6 秒。26 句约 2.5 分钟。

### Step 7 · 编排

```bash
.tools/py/pipeline/bin/python .opencode/skills/video-producer/scripts/plan_edit.py
```

这一步是整条线的"导演"。它做四件事：

1. **按语义选镜**：把文案里的卖点词和画面的「主体/动作/细节」对上
2. **锁主体**：一句话拆成两镜时，第二镜必须还是同一个商品
3. **控冗余**：画面指纹太像的镜头会被扣分，同素材最多用 2 次
4. **定转场**：测出点附近的运动量，运动中 → 溶解，静止 → 硬切

产出 `work/edit-plan.json`。**这个文件可以直接手改**，改完只重跑渲染。

### Step 8 · 渲染

```bash
.tools/py/pipeline/bin/python .opencode/skills/video-producer/scripts/render.py --name 成片名
```

三段式渲染：逐镜头归一化（裁切/缩放/调色/帧率）→ 转场拼接 → 混音 + 烧字幕 + 封装。

### Step 9 · 质检 + 审阅

```bash
.tools/py/pipeline/bin/python .opencode/skills/video-producer/scripts/qc.py --input output/成片名.mp4
.tools/py/pipeline/bin/python .opencode/skills/video-producer/scripts/review.py --input output/成片名.mp4
```

- `work/qc/qc-report.json`：所有指标 + 每条问题的修复建议
- `work/review/contact-sheet.jpg`：逐镜头缩略图
- `work/review/transitions.jpg`：每个转场的前/中/后三帧
- `work/review/shot-table.md`：逐句对齐表 + 镜头明细

### Step 10 · 验收

先自己看这三样，再看成片：

1. 缩略图里有没有镜头把商品裁掉了
2. 转场对照里有没有糊成一团或闪白
3. 对齐表里有没有"说的和画的不是一回事"

都没问题，再把片子交给客户/用户确认。

---

## 5. 文案怎么写（新手最容易翻车的地方）

### 结构骨架

```
钩子(1-3句) → 反向筛选(4-5句) → 产品引入(6句)
→ 功能卖点(7-15句) → 颜色款式(16-20句) → 促单(21-26句)
```

参考片实测：26–29 句、每句 1.4 秒左右、全程无空镜。

### 三个自检问题

写完一句，依次问：

1. **这句话里有具体名词吗？** 没有（如"挖到宝了"）就随便配都行，不计入命中率。
2. **有的话，画面能证明吗？** 拿这个词去素材索引里搜。
3. **搜不到怎么办？** 换说法，别硬剪。比如没有背面镜头，就把"美背设计"改成"边缘走线平整"。

### 正例 / 反例

| 反例 | 问题 | 改法 |
|---|---|---|
| 豹纹上身又纯又欲 | 没有真人镜头 | 豹纹配色又纯又欲 |
| 后背是美背设计 | 没有背面镜头 | 边缘走线你看多平整 |
| 面料是软糯奶皮质感 | 没有面料特写 | 杯垫是裸色的很服帖 |
| 侧边加宽藏住副乳 | 没有侧视镜头 | 底围加宽一点都不勒肉 |

> 这不是文字游戏。文案说"后背"，画面给的是正面，观众一眼就出戏。

---

## 6. 质量是怎么保证的

三道闸门，任何一道不过都不算交付。

### 闸门 1：机器自检

`qc.py` 对照 `reference/quality-bar.md` 打分，输出三类指标：

| 类别 | 指标 | 达标线 | 本项目实测 |
|---|---|---|---|
| 红线 | 响度 | -16 ~ -12 LUFS | -14.29 |
| 红线 | 真峰值 | ≤ -0.5 dBTP | -1.13 |
| 红线 | 黑帧 | 0 | 0 |
| 红线 | 音画时长偏差 | ≤ 0.4s | 0.0 |
| 结构 | 镜头数 | 22–32 | 27 |
| 结构 | 平均镜长 | 1.3–1.9s | 1.76s |
| 结构 | 溶解占比 | 40%–75% | 73% |
| 结构 | 画面相似度 P90 | ≤ 0.48 | 0.47 |
| 语义 | 命中率 | ≥ 60% | 73% |
| 语义 | 相邻同素材 | 0 | 0 |

不达标时，报告里每条都带 `fix`（怎么改）和 `rerun`（重跑哪个阶段），不用猜。

### 闸门 2：证据自检

`review.py` 生成三张图，必须逐张看过：

- 缩略图：镜头选择合不合理
- 转场对照：过渡自不自然
- 对齐表：文案和画面对不对得上

### 闸门 3：用户验收

把成片 + 证据 + 指标一起交给用户，确认后才算交付。
被否掉时按反馈定位到阶段，只重跑受影响部分。

### 验收后要留痕

把这一轮被否掉的原因写进 `reference/lessons.md`。下次开工前先读。
**这是这条流水线唯一的"学习"机制**——它不会自动变聪明，靠的是把人工反馈沉淀成先验。

---

## 7. 目录与产物

```
视频剪辑/
├── 学习素材/              原始素材（只读，不修改）
├── 参考视频/              参考成品 + 抓取元信息
├── work/                  全部中间产物（可删，删了要重跑）
│   ├── reference/         风格规格
│   ├── material/          素材索引 + 画面指纹
│   ├── script.json        文案（你写的）
│   ├── tts/               逐句配音
│   ├── edit-plan.json     最终时间线（可手改）
│   ├── render/            中间片段、音轨、字幕
│   ├── qc/                质检报告
│   └── review/            审阅证据
├── output/                成片
├── .tools/                依赖与模型（约 11GB，不入库）
└── .opencode/skills/video-producer/
    ├── SKILL.md           AI 执行手册（流程 + 闸门）
    ├── README.md          本文件
    ├── scripts/           各阶段脚本
    ├── lib/               公共库
    ├── schemas/           JSON 契约
    ├── templates/         文案模板与写作规范
    └── reference/         方法论 / 质量基线 / 经验沉淀
```

**哪些能删、哪些不能删：**

| 目录 | 能删吗 | 说明 |
|---|---|---|
| `work/` | 能 | 删了重跑，但视觉模型标注要重新算（约 10 分钟） |
| `.tools/` | 能 | 删了要重新 bootstrap |
| `output/` | 能 | 成片，建议留 |
| `学习素材/` | **不能** | 原始素材，删了无法恢复 |
| `参考视频/` | 不能 | 风格来源 |

---

## 8. 出问题了怎么办

| 现象 | 原因 | 处理 |
|---|---|---|
| `bootstrap.sh` 卡住 | 网络慢，在下模型 | 耐心等，或先 `--no-tts` |
| `doctor.py` 报 ffmpeg 缺滤镜 | 用了精简版 ffmpeg | 脚本会自动优先用 ffmpeg-full，确认 `/opt/homebrew/opt/ffmpeg-full` 存在 |
| 字幕中文变方块 | libass 找不到字体 | `fc-cache -f`，或在 `script.json` 里换 `caption.fontFamily` |
| 抓参考片为空 | 抖音风控 | 加 `--headed` 人工过验证 |
| `analyze_material.py` 直接失败 | 视觉模型没起 | 按报错提示跑 `bootstrap.sh` 或 `ollama serve && ollama pull qwen3-vl:4b-instruct` |
| 语义命中率低 | 文案超出素材 / 规则漏词 | 先改文案，再补 `plan_edit.py` 的 `ACTION_RULES` / `COLOR_WORDS` |
| 成片时长比计划短很多 | 转场链越界 | 重跑 `plan_edit.py`（已做帧对齐，正常不会出现） |
| 某个镜头不对 | 选镜问题 | 直接改 `edit-plan.json` 里该镜头的 `clipId/sourceIn/sourceOut`，重跑渲染 |
| 配音太慢/太快 | 语速不合适 | 改 `script.json` 的 `voice.speed`，从配音那步重跑 |
| 手在收商品的画面停留太久 | 尾部延长过多 | 调小 `plan_edit.py` 的 `MAX_TAIL_EXTEND` |
| 转场太多看着糊 | 运动阈值太低 | 调高 `MOTION_CUT_THRESHOLD` |

---

## 9. 注意事项与红线

### 版权

- **BGM**：必须用你已获授权的音轨。脚本只负责循环、闪避、淡出，不提供音乐。
- **字体**：默认用系统 PingFang SC，商用请确认授权。
- **肖像**：素材里出现真人，需本人同意用于商业传播。
- **参考片**：只学结构和节奏，不搬运画面和文案。

### 平台规则

- 用了 AI 配音，**成片必须在画面里标注**（如"本视频由AI配音 非真人发声"）。
  `analyze_reference.py` 会检测参考片有没有这类角标，有就自动写进字幕。
- 不要写素材证明不了的功效（显瘦、聚拢、透气），这类表述容易被判虚假宣传。

### 操作红线

- 不要直接改 `学习素材/` 里的原片，流水线只读它。
- 不要在 `work/` 里手动放文件，会被下一阶段覆盖。
- 改 `edit-plan.json` 时别动 `frames` / `renderFrames`，那是帧对齐用的，改了会音画不同步。
- 换素材后**必须重跑素材体检**，否则指纹和标注还是旧的。

---

## 10. 进阶用法

### 只改一句文案，快速重出片

```bash
# 改 work/script.json 里那一句
PY=.tools/py/pipeline/bin/python
S=.opencode/skills/video-producer/scripts

$PY $S/tts.py          # 重新配音（26 句都重配，约 2.5 分钟）
$PY $S/plan_edit.py    # 重新编排（10 秒）
$PY $S/render.py --name final
```

### 只改一个镜头

直接编辑 `work/edit-plan.json` 里对应 shot 的 `clipId` / `sourceIn` / `sourceOut`，然后：

```bash
.tools/py/pipeline/bin/python .opencode/skills/video-producer/scripts/render.py --name final
```

### 换素材

1. 替换 `学习素材/` 里的文件
2. 重跑 `analyze_material.py`（必须，否则指纹是旧的）
3. 重跑 `plan_edit.py` → `render.py`

### 换参考片

重跑 `fetch_reference.mjs` → `analyze_reference.py` → `plan_edit.py` → `render.py`。
文案和配音不用动（除非参考片节奏差异很大，需要重新配速）。

### 换音色（克隆自己的声音）

1. 录 10–30 秒干净人声，放到 `.tools/cosyvoice/voices/my.wav`
2. 在 `script.json` 里改：

```json
"voice": {
  "spkId": "my_voice",
  "promptWav": "my.wav",
  "promptText": "You are a helpful assistant.<|endofprompt|>你录的那段话的文字",
  "speed": 1.3
}
```

3. 重跑 `tts.py`。首次会缓存这个音色，之后直接复用。

### 加 BGM

把授权音轨放到 `assets/bgm/`，在 `script.json` 里加：

```json
"bgm": { "file": "assets/bgm/xxx.mp3", "gainDb": -20, "fadeOutSec": 1.2 }
```

渲染器会自动循环到成片长度、人声出现时压低、结尾淡出。

### 允许裁切换冲击力

默认不裁切（跟随素材比例）。如果客户接受裁切：

```bash
$PY $S/plan_edit.py --canvas reference   # 跟参考片比例，自动做主体感知裁切
```

### 常用调参

| 想要 | 改哪里 |
|---|---|
| 画幅 | `plan_edit.py --canvas / --height` |
| 每句留白 | `plan_edit.py --gap` |
| 语速 | `script.json` → `voice.speed` |
| 画面去重强度 | `plan_edit.py` → `DIVERSITY_WEIGHT` / `SIM_BASELINE` |
| 收手段落长度 | `plan_edit.py` → `MAX_TAIL_EXTEND` |
| 溶解时长 | `plan_edit.py` → `DISSOLVE_SEC` |
| 转场灵敏度 | `plan_edit.py` → `MOTION_CUT_THRESHOLD` |
| 最小镜长 | `plan_edit.py` → `MIN_SHOT_SEC` |
| 字幕字体 | `script.json` → `caption.fontFamily` |
| 调色 | `plan_edit.py` → `DEFAULT_GRADE` |

---

## 11. 环境变量

所有路径可覆盖，换机器/换目录不用改代码：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VIDEO_PIPELINE_ROOT` | 自动向上查找 `.opencode` | 项目根 |
| `VIDEO_PIPELINE_MATERIAL` | `学习素材` | 原始素材目录 |
| `VIDEO_PIPELINE_REFERENCE` | `参考视频` | 参考片目录 |
| `VIDEO_PIPELINE_WORK` | `work` | 中间产物 |
| `VIDEO_PIPELINE_OUTPUT` | `output` | 成片 |
| `VIDEO_PIPELINE_FFMPEG` | `/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg` → `ffmpeg` | 必须支持 libass |
| `VIDEO_PIPELINE_VLM_URL` | `http://127.0.0.1:11434` | Ollama 地址 |
| `VIDEO_PIPELINE_VLM_MODEL` | `qwen3-vl:4b-instruct` | 视觉模型 |
| `COSYVOICE_THREADS` | `8` | TTS 线程数 |

---

## 12. 性能与资源

### 实测耗时对比

| 项目 | MacBook Air M3 / 24GB（无风扇） | Mac Studio M5 / 32GB |
|---|---|---|
| 素材体检（41 个 / 7.5 分钟） | 18–22 分钟（首次） | 10 分钟（首次） |
| 参考片拆解（40 秒） | 8–15 分钟（首次） | 5–10 分钟（首次） |
| 配音 26 句 / 45 秒 | 5–7 分钟 | 2.5 分钟 |
| 渲染 47 秒成片 | 1–2 分钟 | 35 秒 |
| 质检 + 审阅图 | 10–15 秒 | 20 秒 |
| **首次出片合计** | **30–45 分钟** | **18–25 分钟** |
| 只改画面重出 | 1–2 分钟 | 1 分钟 |
| 只改文案重出 | 5–7 分钟 | 3–4 分钟 |

> 视觉分析和配音都是 CPU 满载，无风扇机型会降频。建议首次运行时插电、关掉其他大应用。

### 磁盘与内存

| 项目 | 数值 |
|---|---|
| 依赖总占用 | 约 11GB |
| 单条成片 | 30–40MB |
| 素材 + 中间产物 | 约为素材体积的 1.5 倍 |
| 建议预留 | 20GB 以上 |
| 峰值内存 | 6–8GB（24GB 机器足够） |

### 低配机器怎么调

`pipeline.conf` 里有三个开关：

```ini
segments_per_clip=6   # 每段素材保留几个候选片段，默认 8，少一点更快
tts_threads=6         # 配音线程，8 核机器留 2 核给系统
fast_analysis=0       # 1 = 跳过画面描述，快约 1/4，匹配稍弱
```

## 13. 已知边界

- **默认不裁切**：画幅跟素材走。素材是 3:4 就出 3:4，不会为了竖屏把两边切掉。
- **只做硬切与溶解**：没有推拉、缩放、抖动特效；参考片本身也是硬切为主。
- **静态图只做定帧**：HEIC/JPG 会当 1.2 秒固定镜头，不做 Ken Burns。
- **视觉模型是 4B 小模型**：能认商品、颜色、明显动作，认不出细微工艺差异。
- **没有自动学习**：不会因为被否掉就自己变聪明，靠 `lessons.md` 人工沉淀。
- **仅 macOS**：`bootstrap.sh` 只支持 macOS；其他系统需要手动装依赖。

---

## 14. 术语表

| 词 | 意思 |
|---|---|
| **风格规格** `style-spec.json` | 从参考片里量出来的客观参数：画幅、镜长分布、字幕位置、响度 |
| **素材索引** `material-index.json` | 每段素材切成可用片段后的清单，带质量分和语义标注 |
| **画面指纹** `signatures.json` | 每段画面的数值特征，用来判断两个镜头是不是"看起来一样" |
| **时间线** `edit-plan.json` | 最终剪辑决策：每句配哪个镜头、从第几秒到第几秒、怎么转场 |
| **语义命中** | 文案里的卖点词，在画面标注里找到对应证据 |
| **溶解** | 两个画面交叉淡入淡出，用于运动中的切点 |
| **闸门** | 交付前必须通过的三道检查：机器质检、证据自检、用户验收 |
| **RTF** | Real Time Factor，合成 1 秒音频需要的时间倍数；1.9 表示比实时慢 1.9 倍 |
