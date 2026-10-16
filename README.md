# wxcopy · 视频号文案流水线

[![CI](https://github.com/xuaj/wxcopy/actions/workflows/ci.yml/badge.svg)](https://github.com/xuaj/wxcopy/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

把对标视频批量变成自己的原创口播短视频，四个阶段一条链跑完：

```
原始文案 ─(1 收集)─> 文案表 ─(2 仿写)─> 仿写文案 ─(3 配音)─> mp3 ─(4 合成)─> mp4
   ↑                   ↑                  ↑                ↑              ↑
四种来源          faster-whisper     Ollama / 云厂商   edge-tts / 云TTS  背景 + 字幕 + BGM
见下表            云端 ASR 可选       可随时切换模型     离线引擎兜底   (图片/视频/纯色)

成片之后还有两个可选环节：封面生成（`cover`）与发布素材包（`publish`）。
```

**原始文案的四种来源**（阶段一，以前只能是音视频）：

| 来源 | 怎么用 | 典型场景 |
|---|---|---|
| `media` 音视频 | 选一个装 mp4/mp3 的目录 | 扒对标账号的口播 |
| `text` 手输 | 直接在框里写/粘一段话 | 自己有想法，先写下来再让 AI 扩 |
| `image` 图片 OCR | 选截图、长图、商品详情页 | 同事发来一张图；同行发的文案海报 |
| `table` 表格文档 | 选 Excel/CSV 的某一列 | 运营已经整理好一批待改写文案 |

---

## 一、安装

```bash
# 1) 建虚拟环境
python -m venv .venv && .venv\Scripts\activate      # Windows
# python3 -m venv .venv && source .venv/bin/activate  # macOS/Linux

# 2) 装依赖
pip install -r requirements.txt

# 3) 开发模式安装（可选，装完能直接用 wxcopy 命令）
pip install -e .
```

> 跑测试/做开发请顺带装：`pip install -e ".[dev]"`
> 要出封面请装 Pillow：`pip install -e ".[cover]"`（其余能力都是按需装，见下表）

### 外部依赖

| 能力 | 依赖 | 说明 |
|---|---|---|
| 本地转写 | `faster-whisper` | 首次运行会从 HuggingFace 下载模型（small≈500MB）。国内慢可设 `HF_ENDPOINT=https://hf-mirror.com` |
| 本地转写解码 | `av`（自动安装） | 自带解码器；解不了的容器会自动调用 ffmpeg 抽音轨 |
| 视频合成 | `ffmpeg` | 系统没装时自动用 `imageio-ffmpeg` 内置二进制（已含 libass / libx264 / aac，够用）。**该内置包不含 ffprobe**，时长探测会自动退回用 ffmpeg 解码，精度一致 |
| 大字字幕断行 | `jieba`（可选） | 按词断行更自然；没装会自动退回按字数切 |
| 图片 OCR | `rapidocr-onnxruntime`（可选） | 本地离线中文 OCR，纯 CPU 可跑；没装时 `wxcopy ocr --engines` 会提示怎么装 |
| 封面生成 | `Pillow`（可选） | 纯本地绘图，不走 AI 生图。没装时 `wxcopy cover` 会提示 `pip install -e ".[cover]"` |
| 中文字幕 | 本机字体 | 默认 `Microsoft YaHei`；Linux/macOS 请把 `video.font` 改成已装字体名。封面会自动探测系统里的中文字体 |
| 本地大模型 | [Ollama](https://ollama.com) | 需本机启动，默认 `http://127.0.0.1:11434` |
| 配音 | `edge-tts` | 免费，需联网。顺带产出**逐字时间戳**（旁挂 `<名字>.cues.json`），字幕靠它对齐真实语音 |
| 背景音乐 | 任意 mp3/wav/m4a | 自带一段 `assets/bgm_demo.mp3`（由 `scripts/make_demo_bgm.py` 合成，无版权问题）可直接试 |

---

## 一·五、Web 控制台（推荐新手用）

一条命令启动，浏览器里点着就能跑完整条流水线（含封面与发布素材包）：

```bash
wxcopy web                      # 默认 http://127.0.0.1:8765，自动开浏览器
wxcopy web --port 9000          # 换端口
wxcopy web --no-browser         # 不自启浏览器，自己打开链接
```

Web 依赖没装时先装一下：

```bash
pip install "fastapi>=0.115" "uvicorn[standard]>=0.30"
# 或 pip install -e ".[web]"
```

界面能做的事：

| 区块 | 说明 |
|---|---|
| 1 选择原始文案来源 | 四类切换：**音视频目录**（转写）、**手输文案**（文本框直接写/粘，实时显示字数）、**图片 OCR**（选图或目录 + 挑 OCR 引擎）、**Excel/CSV**（选文件后自动读出列名，自动猜中文案列，可再选「命名列」）。输出目录、处理数量、递归开关也在这里 |
| 2 选择模型 | 自动探测所有后端：本地 Ollama（列出已装模型）+ 各云厂商。选中厂商后自动列出它的模型；可调温度与并发 |
| 3 仿写风格 | 下拉选择 7 套内置模板（爆款口播/本地商家/促销转化/种草测评/知识科普/情绪共鸣/default） |
| 4 配音设置 | 引擎（edge/云端/离线）+ 音色，音色列表联网自动拉取 |
| 5 输出视频 | 勾上即合成口播视频。**背景可点「浏览…」直接选图片或视频文件**（也支持手写路径），留空则用纯色。字幕可选**经典**（底部整句）或**大字**（中上部 + 关键词高亮，带高亮方式/颜色/关键词/每行字数/每屏行数/打字机节奏）；再加画面尺寸、cover/contain、尾部留白、淡入、CRF、合成并发。**大字样式下可勾选「打字机效果」**，并选自适应（跟语音同步）或固定每字秒数 |
| 5·背景音乐 | 勾上「添加背景音乐」后可选音频文件（自动循环铺满），调音量、闪避开关、闪避强度、结尾淡出秒数 |
| 6 封面 | 勾上即逐条生成 1080×1920 竖图。选风格（科技/纯色/浅色）、背景图、强调色（取色器 + 十六进制双向同步）、底色、页脚、字体、标题/副标题取自哪一列、最长字数、PNG/JPG。**右下角可实时预览**——改完参数点「生成预览」当场看图，不用先跑一遍任务 |
| 7 发布素材包 | 勾上即生成标题候选 / 简介 / 话题并导出 `publish_plan.xlsx`。可调目标平台、文案模板、候选标题数、话题数、简介字数、并发；可选「清单里带上封面路径」和「打包成文件夹」。**这一阶段不发视频**，只把上传前要填的字准备好 |
| 8 转写设置 | Whisper 模型尺寸与语言 |
| 运行区 | 实时进度条、阶段、彩色日志流；可随时取消；完成后列出产物（表格 / 音频 / 视频 / 封面 / 发布清单），封面直接显示缩略图，支持「打开目录 / 打开文件 / 下载」 |

后端接口：`GET /api/browse?path=&kind=`（`kind` 取值 `media` / `video` / `audio` / `image` / `table` / `bg` / `any`）、
`/api/models`、`/api/prompts`、`/api/voices`、`/api/ocr`（OCR 引擎可用性）、
`POST /api/table/columns`（读出表格列名，供前端选列）、`/api/defaults`，
`POST /api/cover/preview`（按当前参数实时出一张封面 PNG，不落盘到产物目录），
`POST /api/run`、`GET /api/job/{id}`、`POST /api/job/{id}/cancel`，
`POST /api/open`（用文件管理器打开目录/文件）、`GET /api/download?path=`。
接口文档在 http://127.0.0.1:8765/api/docs 。

进度是从流水线日志里解析的（`[n/m] 转写` / `仿写进度 n/m` / `配音进度 n/m` / `合成进度 n/m`），
所以不需要改 pipeline 的既有签名。服务只监听 127.0.0.1，外部机器访问不到。

> **一次任务里封面的顺序**：`封面 → 发布素材包`（发布要引用封面路径，只能这个顺序）。
> 所以首次运行时「推荐标题」列还不存在，封面会自动从「仿写文案」里截一句当标题。
> 想让封面用上大模型起的标题，**再点一次「开始处理」**即可——第二次会整段跳过已完成的步骤，只重画封面。

---

## 二、功能模块（四阶段 + 封面 + 可选发布包）

### 模块 1：收集原始文案（`source`）——四种来源

以前入口只能是「一个装视频的目录」，现在四类都行，后面仿写/配音/合成完全一样。

```bash
# ① 音视频转写（原来的能力）
wxcopy source --from media --media D:\data\test -w ./output

# ② 手输一段文案
wxcopy source --from text --text "小店没客流怎么办？三招把路人变成回头客……" --name 门店引流

# ③ 图片 OCR：单张图、或整个目录（目录里每张图各出一条）
wxcopy source --from image --image D:\素材\文案长图.png
wxcopy source --from image --image D:\素材\海报目录 -w ./output

# ④ Excel / CSV 的某一列
wxcopy source --from table --table D:\data\待仿写文案.xlsx --column 原始文案 --name-column 标题
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--from` / `-f` | `media` / `text` / `image` / `table` |
| `--media` / `-m` | 音视频文件或目录 |
| `--text` / `-t` | 手输文案；`--name` 给它起名（留空取前 14 字） |
| `--image` / `-i` | 图片文件或目录，**可重复传多次** |
| `--table` / `--column` | 表格路径 + 文案列名（或 1 起的列序号）；`--name-column` 指定命名列 |
| `--sheet` | 指定工作表名 |
| `--ocr-provider` / `--ocr-model` | 临时换 OCR 引擎 / 视觉模型 |
| `--limit` / `--recursive` | 只处理前 N 个 / 递归子目录 |

产物里的「文件原名」会带来源前缀，方便回溯：`手输_xxx`、`图片_xxx`、`表格_xxx`。

#### 图片 OCR 引擎

`wxcopy ocr --engines` 看哪些能用：

| `--ocr-provider` | 依赖 | 特点 |
|---|---|---|
| `rapidocr` | `pip install "wxcopy[ocr]"` | **本地离线**，中文效果好，纯 CPU、模型才几 MB，推荐 |
| `tesseract` | `pip install pytesseract` + 系统装 tesseract | 老牌，中文要自己装 `chi_sim` 语言包 |
| `ollama` | `ollama pull qwen2.5vl:7b` 之类 | 本地视觉大模型，字迹潦草/复杂版式更强 |
| `openai` | 配 `ocr.base_url` + `ocr.api_key_env` | 各家云厂商的视觉模型，需联网 |
| `auto`（默认） | — | 按上表顺序挑第一个**真能用**的，本地优先 |

想单独试 OCR 效果：

```bash
wxcopy ocr D:\素材\文案长图.png          # 直接打印识别结果
wxcopy ocr D:\素材\目录 -o out.txt       # 整个目录，结果写文件
wxcopy ocr --engines                     # 只看引擎可用性
```

> `auto` 会真的跑一次可用性检查（导入 + 找程序/查模型），
> 不会出现「列表里显示可用、一跑就报错」。

### 模块 1b：语音转文字（`transcribe`）

等价于 `wxcopy source --from media`，保留是为了兼容老脚本。

```bash
# 转写整个目录
wxcopy transcribe D:\data\test -w ./output

# 只跑前 3 个试试效果，换个更大的模型
wxcopy transcribe D:\data\test --limit 3 --model medium

# 用云端转写（省显卡）
wxcopy transcribe ./videos --provider openai

# 常用参数
#   --model small|medium|large-v3   模型越大越准越慢
#   --language zh                   指定语言；留空自动检测
#   --recursive                     递归子目录
#   --no-srt                        不生成 .srt 字幕
#   --force                         忽略已有结果全部重跑
```

产物：

- `output/transcripts.xlsx` —— 列：`序号 / 文件原名 / 时长(秒) / 语言 / 转写文案 / 文案文件`
- `output/text/<文件名>.txt` 与 `.srt`

### 模块 2：文案仿写（`rewrite`）

**a. 读取 Excel/CSV 的文案列**

```bash
# 读取文案列，仿写结果写回「仿写文案」列
wxcopy rewrite ./output/transcripts.xlsx --column 转写文案

# 甲方给的 Excel 也行，指定列号或列名
wxcopy rewrite 甲方文案.xlsx --column 3 --out 仿写结果.xlsx

# 追加个性化要求
wxcopy rewrite in.xlsx --column 文案 --length "150字左右" \
    --tone 接地气 --audience "餐饮小店老板" --cta "评论区扣 1 领方案"
```

**b. 切换模型：本地 Ollama / 各家云厂商**

```bash
wxcopy models                                   # 看看哪些后端可用、有哪些模型

# 本地（默认）
wxcopy rewrite in.xlsx --column 文案 --provider ollama --model qwen3:8b

# 换云厂商，只改一个参数
wxcopy rewrite in.xlsx --column 文案 --provider deepseek   --model deepseek-chat
wxcopy rewrite in.xlsx --column 文案 --provider dashscope  --model qwen-plus
wxcopy rewrite in.xlsx --column 文案 --provider zhipu      --model glm-4-flash
wxcopy rewrite in.xlsx --column 文案 --provider moonshot   --model moonshot-v1-8k
wxcopy rewrite in.xlsx --column 文案 --provider siliconflow --model Qwen/Qwen2.5-72B-Instruct
wxcopy rewrite in.xlsx --column 文案 --provider doubao
```

云厂商需要 API Key，写在环境变量里（复制 `.env.example` 为 `.env` 后填写）：

| 厂商 | 环境变量 |
|---|---|
| DeepSeek | `DEEPSEEK_API_KEY` |
| 阿里通义 | `DASHSCOPE_API_KEY` |
| 智谱 GLM | `ZHIPU_API_KEY` |
| Kimi | `MOONSHOT_API_KEY` |
| 硅基流动 | `SILICONFLOW_API_KEY` |
| 火山豆包 | `DOUBAO_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |

**换个 prompt 风格**

```bash
wxcopy prompts                                  # 列出所有模板
wxcopy rewrite in.xlsx --column 文案 --prompt 爆款口播
wxcopy rewrite in.xlsx --column 文案 --prompt 本地商家     # 行业向模板
wxcopy rewrite in.xlsx --column 文案 --prompt-file ./my.md # 用自己的提示词
```

内置：`default / 爆款口播 / 种草测评 / 知识科普 / 情绪共鸣 / 本地商家 / 促销转化`

**其他**

- `--concurrency 4` 并发数（云端可开 4~8，本地小模型建议 1）
- `--key-column 文件原名` 按某列匹配行，行序变化也不写错
- 断点续跑：目标列已有内容的行自动跳过；`--force` 强制重跑

### 模块 3：文案转配音（`speak`）

```bash
wxcopy voices                                   # 列出中文音色
wxcopy speak ./output/transcripts.xlsx --column 仿写文案 --voice zh-CN-XiaoxiaoNeural

# 换个更沉稳的男声、语速加快
wxcopy speak in.xlsx --column 仿写文案 --voice zh-CN-YunjianNeural --rate +10%

# 用云端 TTS（硅基流动 CosyVoice 等）
wxcopy speak in.xlsx --column 仿写文案 --provider openai
```

常用中文音色：`zh-CN-XiaoxiaoNeural`（女·自然）、`zh-CN-YunxiNeural`（男·年轻）、
`zh-CN-YunjianNeural`（男·沉稳）、`zh-CN-XiaoyiNeural`（女·活泼）、`zh-CN-liaoning-XiaobeiNeural`（东北）。

产物：`output/voice/001_xxx.mp3`，同时把路径回写到表格的「配音」列。

### 模块 4：仿写文案转视频（`video`）

用「仿写文案 + 配音」合成带字幕的竖屏短视频，背景由自己选：**图片、视频、或纯色**。

字幕有**两套样式**，用 `--style` 切换：

| 样式 | 观感 | 适用 |
|---|---|---|
| `classic`（默认） | 底部居中，一行十几个字，白色细描边 | 信息型口播，字幕只是辅助 |
| `big` | **中上部超大粗体短句，每行 2~4 字，隔行或关键词染霓虹绿** | 复刻主流爆款口播号的视觉 |

`big` 样式的默认参数是照着实测爆款视频采样定的：字号＝画面宽 × 14.7%（1080 → 159px）、
行距＝字号 × 1.5、首行顶部在画面 16% 处、高亮色 `#00F090` 霓虹绿、绿字配白描边、
默认换用更粗的 `SimHei`（雅黑 Bold 偏细，不像爆款）。

```bash
# 大字 + 隔行高亮（最像参考视频：首行绿、次行白、隔行再绿）
wxcopy video in.xlsx --column 仿写文案 -b 背景.jpg --style big --highlight alternate

# 大字 + 只把指定关键词染绿
wxcopy video in.xlsx --column 仿写文案 -b 背景.jpg --style big --highlight keywords \
    --keyword "免费,省钱,风口,一桌一码" --highlight-color "#00F090"

# 大字 + 自动挑重点（百分比、金额、营销词）
wxcopy video in.xlsx --column 仿写文案 -b 背景.jpg --style big --highlight auto

# 大字，不要高亮，每行 5 字
wxcopy video in.xlsx --column 仿写文案 -b 背景.jpg --style big --highlight none \
    --big-chars-per-line 5
```

四种高亮模式的取色规则：

| `--highlight` | 效果 |
|---|---|
| `none` | 全部白色（默认） |
| `alternate` | **隔行高亮**——首行绿、次行白、第三行绿……实测最接近参考视频 |
| `auto` | 命中数字/百分比/金额，或「免费/省钱/风口/秘诀/翻倍/一定要」等营销词就染绿 |
| `keywords` | 只染 `--keyword` 指定的词（**建议填你的业务词**，顺带提升分词准确度） |

#### 打字机效果（大字一个一个打出来）

`big` 样式可开 `--typewriter`，让大字**逐字累积出现**，而不是整屏一次闪出来。
实现方式是**累计前缀**：每多一个字就多一条 ASS 字幕事件（`想` → `想想` → `想清楚了`），
关键词从**它自己的第一个字**开始就染成高亮色，整行打完后保持到本屏结束。

```bash
# 自适应节奏：按本屏语音时长反推每字秒数，跟口播同步
wxcopy video in.xlsx --column 仿写文案 -b 背景.jpg --style big --highlight alternate --typewriter

# 固定节奏：每字 0.08 秒（约 12.5 字/秒）
wxcopy video in.xlsx --column 仿写文案 -b 背景.jpg --style big --typewriter --type-speed 0.08

# 自适应 + 打完后多停一会儿（停留占本屏 45%）
wxcopy video in.xlsx --column 仿写文案 -b 背景.jpg --style big --typewriter --type-hold 0.45

# 关掉
wxcopy video in.xlsx --column 仿写文案 -b 背景.jpg --style big --no-typewriter
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--typewriter` / `--no-typewriter` | 关 | 开关，**只在 `--style big` 下生效** |
| `--type-speed` | `0` | 每字固定秒数；`0` = 自适应（按本屏语音时长反推） |
| `--type-hold` | `0.3` | 自适应模式下，打完整行后**停留时间占本屏时长**的比例（0~0.9） |
| `--type-min` | `0.045` | 自适应模式下每字时长**下限**（秒），防止字太多时快到看不清 |

节奏怎么选：

- **自适应（`--type-speed 0`）**：每屏时长来自真实语音时间轴（见下节），
  所以「本屏打完」天然就落在「本屏说完」附近。`--type-min` 兜底防止字数多时过快。
- **固定（`--type-speed 0.08`）**：节奏绝对统一，观感更"打字机"，
  但如果整屏打完的时间超过该屏语音时长，代码会自动压缩到本屏 98% 并给个下限警告值。
- 打字机模式下**自动关闭淡入**（`subtitle_fade`）——硬切换逐字出现，带淡入会糊。

**关于断行**：`big` 样式每行只有 2~4 字，纯按字数切会把「有没有想过」切成
「有没有想 / 过，如果」。所以这一样式改用**按词断行**（贪心装箱 + 标点不落行首），
装了 `jieba` 效果更好；没装会自动退回按字数切，不会报错。
内置了一批短视频领域词（小程序、视频号、扫码点餐、私域、引流…），
你也可以用 `--keyword` 把自家专有名词加进分词词典。

```bash
pip install jieba        # 或 pip install -e ".[subtitle]"
```


```bash
# 用一张竖屏图当背景
wxcopy video ./output/transcripts.xlsx --column 仿写文案 --bg D:\素材\背景.jpg

# 用一个视频当背景（会循环播放到语音结束）
wxcopy video in.xlsx --column 仿写文案 -b D:\素材\背景.mp4

# 不要背景文件，用纯色底
wxcopy video in.xlsx --column 仿写文案 --bg-color "#0f172a"

# 横屏 + 不要字幕 + 更高画质
wxcopy video in.xlsx --column 仿写文案 -b bg.jpg --size "横屏 1920x1080" --no-subtitle --crf 18
```

#### 背景音乐（`--bgm`）与自动闪避

加一段 BGM，人声一出现就自动把音乐压低（不用手调关键帧）：

```bash
# 背景音乐（比人声短会自动循环到视频结束）
wxcopy video in.xlsx --column 仿写文案 -b bg.jpg --bgm assets/bgm_demo.mp3

# 调音量 / 关掉闪避 / 改淡出
wxcopy video in.xlsx --column 仿写文案 -b bg.jpg --bgm bgm.mp3 \
    --bgm-volume 0.12 --bgm-duck-ratio 12 --bgm-fade-out 2.5
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--bgm` | 空 | 背景音乐文件；给了它，即使没给 `--bg` 也会触发视频合成 |
| `--bgm-volume` | `0.18` | BGM 相对人声的音量（0~1），0.18 约比人声轻 15dB |
| `--bgm-duck` / `--no-bgm-duck` | 开 | 人声出现时自动压低 BGM |
| `--bgm-duck-ratio` | `8` | 闪避压缩比，越大压得越狠 |
| `--bgm-fade-out` | `1.5` | BGM 结尾淡出秒数，`0` = 不淡出 |

实现上的几个关键点（都是踩出来的）：

- **闪避用 `sidechaincompress`，把人声当侧链**。人声必须先 `asplit` 分成两路再分别喂给
  `sidechaincompress` 和 `amix`——同一路标签被两个滤镜直接消费、且图里还有视频流时，
  ffmpeg 会报 `Stream specifier ... matches no streams`（实测 ffmpeg 7.1；
  同一张图单独跑纯音频却完全正常，所以这个坑很难查）。
- **`amix` 必须 `normalize=0`**：默认会把每路各降一半，人声会莫名其妙变小。
- **`amix` 用 `duration=first`、人声排第一路**：BGM 是 `-stream_loop -1` 的无限流，
  放前面会让输出永不结束。
- **只在有 BGM 时才统一采样格式**（44.1kHz 立体声）。edge-tts 是 24k 单声道、
  音乐多是 44.1k 立体声，不统一没法混；但没 BGM 的场景就不动它，
  免得不需要混音的人音频格式也被改了。
- **BGM 进指纹**：换文件、改音量/闪避/淡出都会让视频重出。而且比的是
  「文件大小 + 修改时间」而不是路径——同名换文件也能被认出来。
  可复跑的验证脚本在 `scripts/verify_bgm.py`（合成素材 + 频谱分析，
  实测人声段 BGM 被压低 21.8dB、静音段 0dB、结尾正常淡出）。

常用参数：

| 参数 | 说明 |
|---|---|
| `--bg` / `-b` | 背景图片（jpg/png/webp/bmp）或视频（mp4/mov/mkv/…）；留空用 `--bg-color` |
| `--size` | `竖屏 1080x1920`（默认）/ `竖屏 720x1280` / `横屏 1920x1080` / `方形 1080x1080`，也可用 `--width --height` 自定义 |
| `--fit` | `cover` 裁满（默认，不留黑边）/ `contain` 居中留边 |
| `--style` | 字幕样式：`classic`（底部整句）/ `big`（中上部大字 + 关键词高亮） |
| `--highlight` | big 样式高亮：`none` / `alternate` / `auto` / `keywords` |
| `--keyword` | `--highlight keywords` 要染绿的词，逗号分隔（同时会加入分词词典） |
| `--highlight-color` | 高亮色，默认 `#00F090`（也接受 ASS 的 `&H00BBGGRR`） |
| `--big-chars-per-line` `--big-lines-per-screen` | big 样式每行字数（默认 4）/ 每屏行数（默认 3） |
| `--no-subtitle` | 不烧录字幕 |
| `--font-size` `--chars-per-line` `--margin-v` | classic 样式：字号 / 每行字数 / 字幕距底部像素 |
| `--tail-pad` | 语音结束后的留白秒数，默认 0.6 |
| `--fade-in` | 开头淡入秒数，默认 0（不淡入） |
| `--bgm` `--bgm-volume` `--bgm-duck` `--bgm-duck-ratio` `--bgm-fade-out` | 背景音乐与自动闪避，见上一节 |
| `--audio-column` | 配音路径所在列，默认「配音」；取不到就按文件名规则去 `voice/` 里找 |
| `-j` | 合成并发数（CPU 转码，建议 1~4） |

产物：`output/video/001_xxx.mp4`，同时把路径回写到表格的「视频」列。
字幕按语音时长自动分行（`。！？；` 优先断句，长句再按字数切）。中途还会在
`output/video/` 留下同名 `.ass` 文件，不满意可以直接改这个字幕文件再手动重跑 ffmpeg。

### 字幕时间轴：按真实语音对齐，而不是按字数猜

配音（`edge-tts`）在合成时会把**每个字的发声时刻**回吐出来，`wxcopy` 把它存成
配音旁边的 `<名字>.cues.json`。合成视频时：

1. 字幕分组（`classic` 的一行 / `big` 的一屏）先按词断行排好；
2. 把「组的第一个字」映射到时间戳里对应的那个字，**该字的真实发声时刻就是这组的出现时刻**；
3. 下一组的时刻就是本组的结束时刻，最后一组铺到音频结束。

之所以不能按字数均分：TTS 在标点处要停顿，字符速率并不均匀。实测一段 235 字的
口播，按字数均分到后半段会**提前 6.8 秒**——字幕在讲下一句，语音还在上一句。
换成逐字时间戳后，14 个整句的最大偏差是 **0.28 秒**（<0.5s 肉眼不可察觉）。

对齐做了两层容错：时间戳是词级时（edge 有时给「视频」而不是「视」「频」），
词内按字数摊开；文案和时间戳差得太多（相似度 <0.6，比如换了配音）则警告并
**退回按字数均分**，不会硬对出一堆错位字幕。

关掉它：`tts.cues: false`。此时不产生 json，字幕退回按字数比例分配。

**时长对齐**：视频长度 = **配音时长 + 尾部留白**，画面会自动跟着语音走，
不会被截断。实现上有两道保障：

1. 背景一律做成"无限长"（图片 `-loop 1`、视频 `-stream_loop -1`、纯色 lavfi 不写 `d=`），
   靠 `-shortest` 让画面跟随音频，**不用 `-t` 硬截断**；
2. 音频先精确探测时长（没有 ffprobe 时用 ffmpeg 解码兜底），
   再用 `apad` 在尾部补 `tail_pad` 秒静音，最后一句字幕不会被突然切断。

每条合成完还会做一次自检，日志里会打出 `音频 18.84s -> 视频 19.44s`；
万一视频比音频短（比如背景文件损坏无法循环），会打一条 `⚠ ... 短于音频` 告警。

**断点续跑靠指纹，不是靠文件是否存在**：每出一条视频，会在旁边写一个
`<名字>.src.txt`，内容是「文案 + 全部渲染参数 + 语音时间轴」的 SHA1 指纹。下次重跑时：

- 文案改了 → 指纹变了 → **重出**（否则成品和表格对不上）
- 只把字幕样式/高亮/打字机节奏改了一下 → 指纹变了 → **重出**（否则你以为参数没生效）
- 重新配音了（换了音色/语速，时间轴变了）→ 指纹变了 → **重出**（否则字幕和声音错位）
- 文案和参数都没动 → 跳过

同理，`stage_tts` 如果发现「该有逐字时间戳却没有」（旧版本录的音），会**自动重录一次**
补上时间戳，而不是傻跳过——否则字幕只能退回按字数均分。

同理，`stage_asr` 不再整表重写：已有表格会按「文件原名」把转写结果**合并**进去，
`仿写文案 / 配音 / 视频` 三列（以及你手改过的内容）都会保留，
不会每跑一次就白烧一遍大模型。

### 模块 5：发布素材包（`publish`）——可选

> **它不发视频。** 视频号、抖音都没有对普通开发者开放的「代用户发布」接口，
> 任何号称能全自动发布的第三方工具都值得警惕。这一阶段负责的是**上传前的最后一步**：
> 把「标题、简介、话题」这些要手动敲的字准备好，再给一份清单。

```bash
# 给成片配标题/简介/话题，并导出清单
wxcopy publish ./output/transcripts.xlsx --column 转写文案

# 顺带把视频+封面+文案复制进独立文件夹，方便整体拖拽上传
wxcopy publish ./output/transcripts.xlsx --column 转写文案 \
  --cover D:\素材\封面.png --bundle

# 指定必带话题；不调模型时纯规则生成（离线也能出清单）
wxcopy publish in.xlsx --column 仿写文案 --keyword "开源,视频号,AI工具"
wxcopy publish in.xlsx --column 仿写文案 --no-llm
```

产物（默认在 `{workdir}/publish/`）：

| 文件 | 用途 |
|---|---|
| `publish_plan.xlsx` | 总清单：推荐标题 / 标题候选 / 简介 / 话题 / 视频路径 / 封面路径 / 时长 / 平台 / 状态 |
| `<名字>.txt` | 单条的发布文案，排版好可直接复制粘贴 |
| `<名字>/`（`--bundle`） | 视频 + 封面 + 文案，一个文件夹对应一条 |
| `<名字>.copy.json` | 机器可读的存档，重跑时用来还原清单（断点续跑） |

同时会把「推荐标题」「话题」回写到文案表，一表看全。

**标题怎么来的**：默认用本地 Ollama 按「提问钩子 / 反常识 / 数字清单 / 痛点直击 /
利益承诺」五种风格各出一条，你挑一条用。模型不可用（`--no-llm`、Ollama 没起、输出解析失败）
会自动退化成规则截取首句，**清单永远出得来**，不会卡在这一步。

**话题标签**会自动补 `#`、去掉内部空格（空格会让平台把后面的字当正文）、去重；
`--keyword` 指定的词一定会出现。

> 封面哪来的？见下一节。`publish` 会按「表格的『封面』列 → `--cover` 指定 → 封面目录里按名字找」
> 的顺序自动带上，所以先跑 `wxcopy cover` 再跑 `wxcopy publish` 就不用额外传参了。

### 模块 6：成片封面（`cover`）

生成 **1080×1920 竖图**封面，用标题 + 副标题 + 页脚排出来。

**纯本地绘图，不走 AI 生图**：渐变 + 光晕 + 网格都是代码画的，
零成本、可复现、无水印、不受网络影响。想用现成背景图就传 `--bg`。

```bash
# 单张：给一段文案，自动提炼标题
wxcopy cover --text "做视频号最费时间的不是拍摄，是文案。四步自动化。" --name 封面

# 单张：直接指定标题与副标题
wxcopy cover --title "开源免费AI工具推荐" --subtitle "wxcopy：本地Ollama免费仿写" \
    --footer "wxcopy 开源项目"

# 批量：整张表出封面，并把路径回写到「封面」列
wxcopy cover ./output/transcripts.xlsx --title-column 推荐标题 --column 仿写文案

# 换风格 / 换强调色 / 输出 jpg（体积小一半）
wxcopy cover in.xlsx --style light
wxcopy cover in.xlsx --style plain --bg-color "#0f172a" --accent "#00F090"
wxcopy cover in.xlsx --jpeg
```

三种内置风格：

| `--style` | 观感 |
|---|---|
| `tech`（默认） | 深蓝渐变 + 底部霓虹光晕 + 细网格，科技感 |
| `plain` | 纯色底（`--bg-color`，默认深藏青） |
| `light` | 浅色底 + 深色字（`--bg-color` 默认近白），适合资讯/知识类 |

| 参数 | 默认 | 说明 |
|---|---|---|
| `--text` / `--title` / `--subtitle` / `--name` | — | 单张模式；`--text` 会取第一句当标题 |
| `--title-column` | `推荐标题` | 批量模式优先用它；该列不存在或为空时，从 `--column` 的文案里提炼 |
| `--column` | `仿写文案` | 批量模式：用来提炼标题的文案列 |
| `--subtitle-column` | 空 | 批量模式：每行各自的副标题列 |
| `--out-column` | `封面` | 回写封面路径的列名 |
| `--footer` | 空 | 页脚小字，一般写账号名/项目名 |
| `--accent` | `#00F090` | 强调色（标题下的分隔线） |
| `--title-max` / `--subtitle-max` | `20` / `24` | 超出即截断 |
| `--font` | 自动 | 字体名或字体文件路径；自动按 SimHei → 雅黑 Bold → PingFang → Noto CJK → 文泉驿 顺序挑 |
| `--jpeg` / `--png` | `--png` | 输出格式 |
| `--force` | — | 忽略已有封面全部重画 |

产物默认在 `{workdir}/cover/`，命名与视频一致（`001_xxx.png`），这样
`publish` 能按名字直接对上。

几个实现细节：

- **断行按「显示宽度」而不是字数**：ASCII 算半个字，所以「开源免费AI工具推荐」
  会切成 `开源免费AI / 工具推荐`，而不是 `开源免费A / I工具推荐`。
- **标点不落行首**：「做视频号最费时间的不是拍摄，是文案」断成
  `做视频号最费 / 时间的不是拍摄，/ 是文案`，逗号跟着上一行走。
- **字号自适应**：标题长短差别很大，硬编码字号必然要么溢出要么小得可怜，
  所以每行都从大往小试到能塞进画面宽度为止。
- **有参数指纹**（`<名字>.cover.txt`）：标题、副标题、任一渲染参数变了就重画，
  一致就跳过——和视频那边同一套规矩，避免「改了参数却看到旧图」。

### 一条龙

```bash
# 只跑到配音
wxcopy run D:\data\test -w ./output --provider ollama --model qwen2.5:7b --limit 5

# 给了 --bg 就顺带把视频也合出来
wxcopy run D:\data\test -w ./output --limit 5 --bg D:\素材\背景.jpg --size "竖屏 1080x1920"

# 只给 --bgm 也会触发视频合成
wxcopy run D:\data\test -w ./output --limit 5 --bg D:\素材\背景.jpg --bgm assets/bgm_demo.mp3

# 也可以不从视频开始：手输 / 图片 / Excel 都支持
wxcopy run --from text  --text "小店没客流怎么办？三招把路人变成回头客" -w ./output
wxcopy run --from image --image D:\素材\海报目录 -w ./output
wxcopy run --from table --table D:\data\待仿写文案.xlsx --column 原始文案 \
           -w ./output --bg D:\素材\背景.jpg --size "竖屏 1080x1920"
```

一条龙目前只跑到视频。封面（`wxcopy cover`）和发布包（`wxcopy publish`）
因为通常要人工挑标题、调版式，设计上留成两条独立命令。

---

## 三、目录结构

```
wxcopy/
├── config/config.example.yaml    # 配置模板（含全部厂商端点预设）
├── .env.example                  # API Key 环境变量示例
├── assets/bgm_demo.mp3           # 自带的演示 BGM（无版权问题，可换）
├── scripts/
│   ├── make_demo_bgm.py          # 生成上面那段 BGM（可复现）
│   └── verify_bgm.py             # 用合成素材 + 频谱分析验证闪避/循环/淡出
├── src/wxcopy/
│   ├── cli.py                    # 命令行入口（typer）
│   ├── settings.py               # 配置加载（YAML + ${ENV} 插值，多份合并）
│   ├── pipeline.py               # 各阶段编排 + VideoOverrides / CoverOverrides
│   ├── core/
│   │   ├── source.py             # 阶段一：四种文案来源 + OCR 后端
│   │   ├── asr.py                # 音视频转写：faster-whisper + 云端 ASR
│   │   ├── llm.py                # 模块2：Ollama + OpenAI 兼容云厂商
│   │   ├── rewriter.py           # 模块2：prompt 模板、清洗、批量并发
│   │   ├── tts.py                # 模块3：edge / openai / pyttsx3
│   │   ├── video.py              # 模块4：背景 + ASS 字幕 + BGM 混音 + ffmpeg 合成
│   │   ├── publish.py            # 模块5：标题/简介/话题 + 发布素材包与清单
│   │   ├── cover.py              # 模块6：封面排版与绘制（Pillow 可选）
│   │   └── media.py              # 抽音轨、时长探测、文件名安全化
│   ├── io/table.py               # Excel/CSV/TSV 读写（openpyxl 原地写列）
│   ├── web/                      # FastAPI 控制台（app.py / jobs.py / static/index.html）
│   ├── prompts/rewrite/*.md      # 可自由编辑的提示词模板
│   └── prompts/publish/*.md      # 发布文案的提示词模板
├── tests/                        # 单元测试（pytest）
├── LICENSE                       # MIT
└── README.md
```

### 数据流

```
                        ┌─ media  音视频 ─→ asr.py
   阶段一 source.py ────┼─ text   手输
   （四种来源）          ├─ image  图片 ──→ OCR 四后端
                        └─ table  表格 ──→ io/table.py
                                ↓
                        transcripts.xlsx  ← 全程唯一的中间态
                                ↓
   阶段二 rewriter.py ──→ llm.py（Ollama / 云厂商）──→ 「仿写文案」列
                                ↓
   阶段三 tts.py ───────→ edge / 云 TTS / pyttsx3 ──→ voice/*.mp3 + *.cues.json
                                ↓
   阶段四 video.py ─────→ ffmpeg + ASS 字幕 + BGM ──→ video/*.mp4
                                ↓
   阶段五 publish.py ───→ llm.py（可关闭）─────────→ publish/ 素材包 + 清单
        （可选，不发视频）

   旁路：cover.py ──────→ Pillow 本地绘图 ──────────→ cover/*.png（publish 自动带上）
```

四阶段之间只通过 `workdir/transcripts.xlsx` 传递状态，所以任一阶段都能单独重跑，
中断后也不会丢已有成果。

## 四、扩展新模型厂商

绝大多数国产厂商都提供 OpenAI 兼容端点，所以**不用改代码**，在 `wxcopy.yaml` 里加一段即可：

```yaml
providers:
  我的厂商:
    type: openai
    base_url: https://api.example.com/v1
    api_key_env: MY_API_KEY
    model: my-model-name
```

然后：

```bash
wxcopy rewrite in.xlsx --column 文案 --provider 我的厂商
```

## 五、常见问题

**转写很慢 / 想要更准**
模型从 `small` 换到 `medium` 或 `large-v3`；有 NVIDIA 显卡时配置里 `asr.device: cuda`、
`asr.compute_type: float16`，速度能提升 5~10 倍。

**faster-whisper 下载模型卡住**
设置环境变量后重试：`set HF_ENDPOINT=https://hf-mirror.com`

**Ollama 连不上**
确认 `ollama serve` 已启动；`wxcopy models` 会显示每个后端的状态。
换模型前先 `ollama pull qwen3:8b`。

**仿写结果不理想**
换更强的模型（本地 `qwen3:8b` → 云端 `deepseek-chat`）；调 `--temperature`（0.6 更稳、1.0 更有创意）；
用 `--prompt-file` 写自己的行业模板。

**edge-tts 报错**
需要联网；若公司网络屏蔽，改用 `--provider pyttsx3`（离线，需 `pip install pyttsx3`）或云端 TTS。

**打字机打得不是快就是慢**
自适应模式下每字时长是「本屏时长 ÷ 本屏字数」，所以**字数多的屏打得快**。
想统一节奏就用 `--type-speed 0.08` 固定每字秒数；只想让快屏不那么赶，就抬高 `--type-min`
（例如 `--type-min 0.07`）。反过来觉得停留时间太长，把 `--type-hold` 从 `0.3` 降到 `0.15`。

**提示没有可用的 OCR 引擎**
先 `wxcopy ocr --engines` 看具体原因。绝大多数情况装一下就好：

```bash
pip install "wxcopy[ocr]"       # 等价于 pip install rapidocr-onnxruntime
```

想用本地视觉大模型就 `ollama pull qwen2.5vl:7b`，然后在配置里写
`ocr.provider: ollama` + `ocr.model: qwen2.5vl:7b`；用云端的写 `openai` 并配
`ocr.base_url` / `ocr.api_key_env`。

**OCR 识别出来的字不对**
合成图（纯色底 + 大号黑体）几乎不会错；真实截图/手写体建议换视觉大模型
（`--ocr-provider ollama` 或 `openai`）。另外把 `--ocr-provider rapidocr` 时
把 `ocr.min_score` 从 `0.5` 调到 `0.3` 能多捞回一些字，代价是错字变多。

**选了 Excel，结果写回哪里**
流水线始终以 `workdir` 下的 `transcripts.xlsx` 为准（不改动你的原文件），
原表格的每一行会被复制进来，「文件原名」带 `表格_` 前缀。
如果你用了 `--name-column`，前缀后面跟的就是那一列的值，方便对回去。

**同一条文案被反复配音/合成视频**
产物命名是「行号_名字」，所以行序必须稳定。表格合并时会沿用旧表行序、
新行追加在末尾，不会把已经做过的行挪位置。如果你手工在表里插了行，
行号会整体顺移，对应的音频/视频会被重做一次——这是预期的。

**打字机打完最后一行就切屏了，看不清**
这是 `--type-hold` 在起作用——它决定「打完到切屏」这段停留占本屏时长的比例，
默认 0.3（30%）。加到 `0.5` 会让最后一屏多停一会儿；同时 `tail_pad` 保证整条视频尾部还有静音留白。

**视频合成报「未找到 ffmpeg」**
`pip install imageio-ffmpeg` 即可（自带 ffmpeg 7.1，含 libass/libx264/aac），
或自己装系统 ffmpeg 并加入 PATH，程序会优先用系统的。

**视频里字幕变成方块**
字体没装在系统里。Windows 默认 `Microsoft YaHei` 一般没问题；
Linux/macOS 把 `video.font` 改成已装的中文字体名（如 `Noto Sans CJK SC`）。

**背景图被裁掉了主体**
`fit: cover` 会裁满画面（不留黑边，但会切边）。想完整显示就改 `--fit contain`，
它会居中留黑边。建议直接用与目标尺寸比例一致的背景图（竖屏 1080×1920 用 9:16 图）。

**合成很慢**
1080×1920 一条约 5~20 秒（CPU）。赶时间可把 `--size` 降到 `竖屏 720x1280`、
`--crf` 提到 26，或 `-j 4` 开并发。这一阶段是纯 CPU 转码，不吃显卡。

**视频比配音短、话没说完就结束了**
已在 v0.1.0 修复（旧版本会按 `文件大小/16000` 粗估音频时长，而 edge-tts 的 mp3 只有约
48kbps，估出来只有真实时长的 1/3，再用 `-t` 硬截断就把音频砍了）。
现在背景无限循环 + `-shortest` 跟随音频，并支持 `--tail-pad` 调整尾部留白。
如果仍然偏短，检查日志里那条自检行 `音频 X s -> 视频 Y s`：
Y 小于 X 会额外打 `⚠` 告警，通常是背景视频本身异常，换张图片背景试试。

**能不能自动发布到视频号 / 抖音？**

不能，wxcopy 也不打算做。**视频号、抖音都没有对普通开发者开放的「代用户发布视频」接口**
（抖音开放平台给的是登录授权、分享组件、小程序、数据开放，且多数要企业资质+审核）。
所以「自动发布」这个环节，任何工具都只能靠模拟操作网页来实现——那样做有验证码风控，
也大概率违反平台服务协议，不建议拿主账号冒险。

wxcopy 负责到**上传前**：`wxcopy publish` 把标题候选、简介、话题、视频、封面整理成
素材包和清单，你按清单到平台手动传即可。真要压这块时间，值得优化的是「批量出片」，
而不是最后那一次点击。

**`publish` 生成的标题不好用**

换更强的模型（`--model qwen3:8b`）；标题候选本来就是给你挑的，不满意就改
`prompts/publish/*.md` 里的要求，或者用 `--prompt-file` 指向自己的模板。
只想先看效果可以用 `--no-llm`，纯规则也会出一份清单。

**末尾有一小段没声音**
那是 `--tail-pad` 的尾部留白（默认 0.6 秒），用来让最后一句字幕停留一下而不是戛然而止。
想紧贴语音结束就设 `--tail-pad 0`。

**加了 BGM 之后听着还是吵 / 人声变小了**
调小 `--bgm-volume`（默认 0.18 已经比人声轻约 15dB，可以试 0.10）。
如果感觉是「人声自己变小了」，那多半不是 BGM 的问题——正常情况不会，
`amix` 里刻意关掉了 `normalize` 就是为了防止这个。可以用
`python scripts/verify_bgm.py` 在你机器上复现一遍量化结果。

**BGM 换成同名文件了，但视频没重新生成**
不会发生。BGM 进指纹时比的是「文件大小 + 修改时间」而不是路径，
同名换文件也会被认出来。真遇到问题就加 `--force`。

**BGM 循环的地方听着有接缝**
`--bgm` 是按原文件首尾相接循环的，所以要么用本身能无缝循环的音乐，
要么用比视频更长的 BGM（不触发循环）。

**封面是空白的 / 生成失败**
两种原因，报错信息会直接说明：

- 缺 Pillow：`pip install -e ".[cover]"`
- 系统里没有中文字体：用 `--font` 指定字体文件路径（如
  `--font "C:/Windows/Fonts/simhei.ttf"`）。Linux 上装 `fonts-noto-cjk` 即可。

**封面标题被截断了**
`--title-max` 默认 20 字，超出会截断。既然标题是给用户一眼看到的，
截断通常比缩小字号更合适；真需要更长就调大 `--title-max`，字号会自动缩。

**封面字太小 / 太大**
字号是按「最长那行能塞进画面宽度」自动算的，所以标题越长字越小。
想让字大一点：`--title-max 12` 或 `--subtitle-max 16` 限制字数，
或者把 `--style` 换成 `plain`（深底纯色，对比更强）。

**字幕比语音快/慢半拍，越到后面越明显**
先看合成日志里那行 `字幕时间轴：...`：

- 显示「按 TTS 逐字时间戳对齐」→ 对齐已生效，偏差通常在 0.3 秒内；
  若仍感觉错位，多半是配音被外部工具重制过（时间戳没跟着更新），
  `wxcopy speak ... --force` 重录一次配音即可。
- 显示「按字数比例分配」→ 说明配音旁边没有 `.cues.json`。
  常见原因是用 `provider: openai`（该引擎不返回字级时间戳）、
  配置里关了 `tts.cues`、或音频是手工放进 `voice/` 的。
  最省事的修法：把 `tts.cues` 打开后 `wxcopy speak ... --force` 重录。

核对方法：视频旁边会留下同名 `.ass`，用任意播放器/字幕工具都能逐句对时。
`wxcopy` 自身也可以：`wxcopy source --from media 成品.mp4` 把语音转回文字，
看时间点是否与字幕对得上。

**大字字幕的字不够粗 / 不像爆款**
`big` 样式默认用 `SimHei`（Windows 自带，比雅黑 Bold 饱满）。
想更粗可以装思源黑体 Heavy / 阿里巴巴普惠体，然后在配置里写：
`video.big_font: "Source Han Sans SC Heavy"`。

**大字字幕把词拆开了（「小程序」变成「小 / 程序」）**
装 `jieba`（`pip install jieba`）启用按词断行；再把你的专有名词填进
`--keyword` 或配置的 `video.keywords`，它们会被加进分词词典。

**大字字幕位置太靠上 / 太靠下**
改配置里的 `video.big_top_ratio`（首行顶部占画面高度的比例，默认 0.16）
或 `video.big_line_spacing`（行距倍数，默认 1.5）。

---

## 六、开发

### 跑测试

```bash
pip install -e ".[dev]"     # pytest + ruff
pytest                      # 全量
pytest -q tests/test_table.py            # 单个文件
pytest -q -k "merge"                     # 按名字挑
```

测试只覆盖**纯逻辑**（表格读写、配置解析、行序合并、视频/封面指纹、断行与排版、
音频滤镜串拼接、文件名安全化），不碰模型调用和 ffmpeg，所以几秒就能跑完、也不需要 API Key。
需要 Pillow / 中文字体的用例会自动跳过，不会因为你机器上没装而变红。

需要真跑 ffmpeg 的验证脚本（不进单测，得手动跑）：

```bash
python scripts/verify_bgm.py      # BGM 循环 / 闪避深度 / 结尾淡出，量化输出
```

新增渲染参数时请留意两条测试会盯着你：

- `test_sig_fields_all_exist_on_videocfg` —— `_SIG_FIELDS` 里写错字段名会立刻失败
  （否则新参数不进指纹，用户改了参数却看到旧片，这是最难查的一类 bug）
- `test_sig_covers_typewriter_params` —— 同类防漏

封面那边同理：`test_cover_sig_covers_every_core_field` 会遍历 `CoreCoverCfg`
的**全部**字段，往 `CoverCfg` 加字段而忘了进指纹就会红。

### 代码规范

```bash
ruff check .        # lint
ruff format .       # 格式化（提交前跑一下）
ruff format --check .   # 只检查不改
```

约定：`line-length = 120`，Python 3.11+ 语法（`X | None` 写法）。

### CI

推送 / PR 到 `main` 会自动跑（见 `.github/workflows/ci.yml`）：

- **ruff lint** + **ruff format --check**
- **pytest**，矩阵：Ubuntu × Python 3.11/3.12，外加 Windows × 3.11
  （Windows 是因为 `safe_stem` 的路径分隔符处理在两套系统上语义不同）

CI 里刻意用 `pip install --no-deps` 跳过了 `faster-whisper`：它会拖进
torch（2GB+），而单元测试全程不碰 ASR / ffmpeg / 模型，装了纯属浪费
（装 torch 的话 CI 要从 1 分钟变成 10 分钟以上）。
测试实际只需要 `pandas / openpyxl / PyYAML / rich` 四个运行时依赖。

### 加一个新阶段

四阶段之间只通过 `workdir/transcripts.xlsx` 传递状态，所以加阶段很直接：

1. 在 `core/` 下写实现；
2. 在 `pipeline.py` 里加一个 `stage_xxx(settings, table, ...)`，读表格 → 干活 →
   用 `write_column()` 把结果回写新列；
3. 在 `cli.py` 加命令，在 `web/app.py` 加开关。

### 改阶段四的参数

渲染参数统一走 `VideoOverrides`（`pipeline.py`），不要再往 `stage_video` 里加
扁平关键字——每加一个参数要改三处签名，很容易漏。

```python
ov = VideoOverrides(style="big", typewriter=True, crf=20)
stage_video(st, table, ov, name_column="文件原名")
```

**注意**：新参数要同时加进 `_SIG_FIELDS`，否则改了它不会重出视频。

---

## 七、合规提醒

本项目定位是**学习对标账号的选题与结构**。仿写默认在提示词里强制「禁止照抄原句」，
但生成结果仍需人工审核后再发布，避免侵犯他人著作权或触发平台原创度判定。
下载的他人视频请勿直接搬运。
