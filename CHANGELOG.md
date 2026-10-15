# 更新日志

本项目用语义化版本：`主版本.次版本.修订号`。

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
