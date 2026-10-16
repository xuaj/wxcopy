# 更新日志

本项目用语义化版本：`主版本.次版本.修订号`。

## [0.5.0] - 2026-10-16

### 新增

- **`wxcopy cover`（模块 6）：成片封面生成**。标题 + 副标题 + 页脚排成 1080×1920 竖图。
  **纯本地绘图，不走 AI 生图**：渐变、光晕、网格都是代码画的，零成本、可复现、无水印。
  - 单张：`wxcopy cover --text "一段文案"`（自动取第一句当标题）或 `--title/--subtitle`
  - 批量：`wxcopy cover in.xlsx`，逐行出图并把路径回写到「封面」列
  - 三种风格 `--style tech|plain|light`，可换强调色、背景图、页脚、字体、jpg 输出
  - 标题取值优先级：表格「推荐标题」列 → 从「仿写文案」列提炼 → 行名
  - `core/cover.py`：`make_cover()` / `make_many()` / `plan_lines()` / `wrap_lines()` / `title_of()`
- **BGM 混音与自动闪避**：`wxcopy video --bgm bgm.mp3`。
  - BGM 比视频短会自动循环铺满；`--bgm-volume`（默认 0.18）调相对音量
  - 人声一出现就自动压低 BGM（`sidechaincompress` 把人声当侧链），`--bgm-duck-ratio` 调压缩比
  - `--bgm-fade-out`（默认 1.5s）结尾淡出；`--no-bgm-duck` 可关掉闪避
  - 新增配置项 `video.bgm` / `bgm_volume` / `bgm_duck` / `bgm_duck_ratio` / `bgm_fade_out`
- `assets/bgm_demo.mp3`：自带的演示 BGM（由 `scripts/make_demo_bgm.py` 合成，无版权问题）。
- `scripts/verify_bgm.py`：用合成素材 + 频谱分析量化验证 BGM 循环/闪避/淡出。
- `publish` 自动找封面：表格「封面」列 → `--cover` → 封面目录里按名字找（配置 `publish.auto_cover`）。
- 可选依赖 `.[cover]`（Pillow）；`paths.cover_dir` 与 `cover.*` 配置段。
- `tests/test_cover.py`（32 条）+ `tests/test_bgm.py`（26 条），共 178 条单测。

### 修复

- 封面标题断行把标点甩到行首（「…不是拍 / 摄，是文案」）：换行前先看下一个字符是不是收尾标点。
- 封面按字数断行导致中英混排被切断（「开源免费A / I工具推荐」）：改按显示宽度（ASCII 算半个字）。
- 封面 `light` 风格的字看不见：底色默认写死成深色，与深色字撞车；改为按风格取默认底色。
- 封面 `tech` 风格死黑：早期把整图 blend 到黑来压网格，把光晕一起压没了；改为在独立透明层上低 alpha 合成。
- `title_of()` 对超长首句会硬截断（「…，本地Oll…」）：改为退一步取第一个逗号前的分句。
- 封面中文串 `wxcopy：...` 的冒号不落行首。

### 说明

- `amix` 必须显式 `normalize=0`（默认会把每路各降一半，人声会莫名变小）；
  `duration=first` 要求人声排第一路，否则无限循环的 BGM 会让输出永不结束。
- 人声必须先 `asplit` 成两路再分别喂 `sidechaincompress` 和 `amix`：同一路标签被两个滤镜
  直接消费、且图里还有视频流时，ffmpeg 会报 `Stream specifier ... matches no streams`
  （实测 ffmpeg 7.1；同一张图单独跑纯音频却正常，属难查的坑）。
- 只在有 BGM 时才把音频统一成 44.1kHz 立体声；没有 BGM 的场景保持原样（24k 单声道），
  避免把不需要混音的人的音轨也改掉。
- BGM 进视频指纹时比的是「文件大小 + 修改时间」而不是路径，同名换文件也能触发重出。

## [0.4.0] - 2026-10-15

### 新增
- **`wxcopy publish`（阶段五，可选）**：成片 -> 发布素材包 + 清单。用 LLM 生成
  5 个候选标题（提问钩子 / 反常识 / 数字清单 / 痛点直击 / 利益承诺五种风格）、
  一段简介、6 个话题标签，导出 `publish_plan.xlsx` 清单，并把「推荐标题 / 话题」回写文案表。
- `--bundle`：把「视频 + 封面 + 发布文案」复制进独立文件夹，方便整体拖拽上传。
- `--cover`（文件或目录）/`--cover-column`：清单里带封面绝对路径。
- `--no-llm`：完全离线也能出清单（规则截取首句 + 关键词当话题），模型挂了会自动降级。
- `--keyword`：话题里必带的词，会注入提示词。
- 配置项 `publish.*`（titles / desc_max / topics / platforms / concurrency / prompt）
  与 `paths.publish_dir`。
- `core/publish.py`：`PublishCopy`、`parse_copy()`、`generate_copy()`、`save_copy()/load_copy()`、
  `plan_row()`、`write_bundle()`。
- `tests/test_publish.py`（37 条），重点覆盖模型输出的脏 JSON（围栏、前后废话、嵌套）。
- README「模块 5：发布素材包」+ 常见问题「能不能自动发布到视频号/抖音」。

### 说明
- **本阶段不发视频**。视频号、抖音都没有对普通开发者开放的「代用户发布」接口，
  模拟网页操作有风控且大概率违反平台协议。wxcopy 只做到「上传前」。

### 修复
- 标题被硬截断成半个词（「口播视」）：提示词要求 ≤20 字，代码只在超 30 字时兜底截断。
- 话题标签含空格（`#AI 仿写`）会被平台当正文截断：`_norm_topics()` 去掉内部空白。
- `--keyword` 形同虚设：现在会注入提示词，而不只是降级时用。
- 清单里的封面路径是相对路径：统一 `resolve()` 成绝对路径。

---

## [0.3.0] - 2026-10-15

### 新增
- **字幕按真实语音对齐**：`edge-tts` 合成时顺手记录每个字的发声时刻，存成配音旁的
  `<名字>.cues.json`；视频阶段据此排字幕，不再按字数均分。
  实测 235 字口播：按字数均分到后半段**提前 6.8 秒**，改用时间戳后 14 个整句
  最大偏差 **0.28 秒**。
- `core/tts.py`：`CUES_SUFFIX` / `cues_path_of()` / `read_cues()` / `read_cues_raw()` /
  `write_cues()` / `supports_cues()`，以及 `AUDIO_EXTS`。
- `core/video.py`：`align_timings()`（组级对齐）+ `_cue_char_times()`（词级时间戳摊成字级）。
- 配置项 `tts.cues`（默认 `true`）。关掉则不产生 json，字幕退回按字数比例分配。
- `pipeline._find_voice()` / `_cues_digest()`。
- `tests/test_cues.py`（25 条）：时间戳读写、对齐算法、词内插值、退化路径。
- README「字幕时间轴」小节 + 常见问题「字幕比语音快/慢半拍」。

### 修复
- **字幕时间轴跑偏**：`alloc_screen_timings()` 按字数比例分配时间，但 TTS 在标点处要停顿、
  字符速率并不均匀，长文案后半段能差出 6 秒以上（字幕已经在讲下一句，语音还在上一句）。
  现在优先用逐字时间戳；拿不到或与文案相似度 <0.6 时才回退，并打警告。
- **`.cues.json` 会被当成配音**：`voice/` 目录里按 `<名字>.*` 找音频时，文件名排序下
  `.cues.json` 排在 `.mp3` 前面。现按音频后缀白名单过滤（3 处调用点统一收敛到 `_find_voice()`）。
- **重新配音后视频不重出**：视频指纹以前只含「文案 + 渲染参数」，换音色/语速后
  时间轴变了但指纹没变，会继续用旧片。现在指纹纳入时间戳摘要（`_cues_digest()`）。
- **旧版本录的配音不会补时间戳**：`stage_tts` 以前只看音频文件是否存在。
  现在「该有逐字时间戳却没有」会自动重录一次，否则字幕只能退回按字数均分。

### 变更
- `stage_video`：先解析配音路径再算指纹（指纹要用到配音的时间戳摘要）。
- `video.py` / `tts.py` 模块 docstring 补充时间轴来源说明。

---

## [0.2.0] - 2026-10-15

### 新增
- 单元测试 `tests/`（57 条），覆盖表格读写、配置解析、行序合并、视频指纹等纯逻辑
- `pyproject.toml` 增加 pytest / ruff 配置
- `.gitignore`、`LICENSE`（MIT）、`CHANGELOG.md`
- `pipeline.VideoOverrides`：把阶段四（`stage_video`）的 30 多个渲染参数收敛成一个对象
- `pipeline._row_name()`：统一「行号_名字」的产物命名，消除 4 处重复

### 修复
- **`.tsv` 表格读取崩溃**：`io/table.py` 只按 `.csv` 判文本表格，`.tsv` 会被送进
  `pd.read_excel` 抛 `ValueError: Excel file format cannot be determined`。
  现在按后缀取分隔符（csv→`,`，tsv→`\t`），写回时同样保持分隔符。
- **`safe_stem` 遇斜杠丢名字**：Windows 下 `Path("门店/引流").stem` 只剩「引流」，
  导致表格「命名列」带 `/` 或 `\` 时产物名被截断。现在先替换分隔符再取 stem。
- 前端选 Excel 后点「开始处理」误报「请先选择视频目录」——`startJob()` 里残留了
  改造前的 `if(!payload.media)` 守卫。已改为按来源类型分别校验（新增 `checkSource()`）。

### 变更
- `stage_video` 签名收敛：渲染参数改走 `overrides`（第 3 个位置参数）。
  旧的扁平关键字调用（如 `stage_video(..., style="big", crf=18)`）仍然可用，
  会经 `_coerce_legacy()` 合并进 `VideoOverrides`。
- `pipeline.py` 的 `hashlib` / `json` / `shutil` 从函数内 import 提到模块顶部。
- 模块 docstring 由「三阶段」更正为「四阶段」。

---

## [0.1.0]

首个可用版本。

### 新增
- 模块 1：音视频转写（`faster-whisper` 本地 + OpenAI 兼容云端 ASR）
- 模块 2：文案仿写（Ollama 本地 / DeepSeek·通义·智谱·Kimi·硅基流动·豆包等云端），
  可断点续跑、并发、按「文件原名」对齐行
- 模块 3：文案转配音（`edge-tts` / 云端 TTS / `pyttsx3` 离线）
- 模块 4：仿写文案转口播视频，背景支持图片 / 视频 / 纯色，字幕烧录（ASS + libass）
- Web 控制台（FastAPI + 单页）：全程点选操作、实时日志与进度、可取消
- 大字字幕样式 `big`（中上部短句 + 关键词高亮）与打字机效果
- 原始文案四种来源：音视频 / 手输 / 图片 OCR / Excel·CSV 表格
- 图片 OCR 四后端：RapidOCR（本地离线）/ Tesseract / Ollama 视觉 / 云端视觉

### 修复
- 视频时长短于配音（旧版按「文件大小 / 16000」粗估音频时长，而 edge-tts 的 mp3
  约 48kbps，估算只有真实时长的 1/3，再用 `-t` 硬截断会把音频砍掉）。
  改为背景无限循环 + `-shortest` 跟随音频，并加 `--tail-pad` 尾部留白。
- 重跑 `stage_asr` 会整表重写，把「仿写文案 / 配音 / 视频」列清空，
  导致每次都要重烧大模型。改为按「文件原名」合并，并严格保持旧行序。
- 视频断点续跑只判断文件是否存在，改了字幕参数不生效。
  改为「文案 + 38 项渲染参数」的 SHA1 指纹（`<名字>.src.txt`）。
