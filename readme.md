# 自动质检流水线

本目录用于处理标注平台导出的 JSONL 数据，完整流程为：

```text
标注 JSONL
   ↓
extract_qc_to_md.py：提取字段、抓取信源
   ↓
ocr_and_write.py：下载截图、执行中英文 OCR
   ↓
llm_judge.py：使用 prompt.py 逐条质检并打标
   ↓
最终 CSV
```

通常只需要运行 `run_all.py`。另外三个脚本适合单独测试、排查问题或从中间阶段继续执行。

---

## 1. 快速开始

### 1.1 安装依赖

建议使用 Python 3.10 或更高版本。

```powershell
pip install aiohttp beautifulsoup4 lxml requests openai crawl4ai
```

说明：

- `aiohttp`、`beautifulsoup4`、`lxml`：普通信源网页抓取；
- `requests`：图片下载和 OCR 请求；
- `openai`：调用 OpenAI 兼容的 LLM 接口；
- `crawl4ai`：普通 HTTP 抓取失败后的浏览器兜底。

### 1.2 配置 `.env`

复制 `.env.example` 为 `.env`，然后填写真实密钥：

```dotenv
OCR_API_KEY=your_ocr_api_key
OCR_ENDPOINT=https://www.evern.ccwu.cc/ocr
LLM_API_KEY=your_llm_api_key
LLM_BASE_URL=https://tokenhub.tencentmaas.com/v1
LLM_MODEL=deepseek-v4-pro-202606
```

`.env` 已被 `.gitignore` 忽略，不要提交或分享真实密钥。

配置优先级：

1. 命令行参数；
2. 当前系统环境变量；
3. `.env`；
4. 脚本内置默认值。

### 1.3 一键执行

在 `自动质检` 目录中运行：

```powershell
python .\run_all.py ".\新任务25条测试.jsonl"
```

默认最终结果：

```text
新任务25条测试_qc_result.csv
```

所有中间产物写入：

```text
data\
```

---

# 2. `run_all.py`：一键全量流水线

`run_all.py` 使用 `subprocess` 串联三个处理脚本。任何阶段返回非零状态时，流水线会立即停止，不会拿不完整的中间结果继续执行。

## 2.1 第一阶段：字段提取和全量信源抓取

执行 `extract_qc_to_md.py`，默认等价于：

```powershell
python .\extract_qc_to_md.py `
  ".\新任务25条测试.jsonl" `
  -o ".\data\新任务25条测试_qc_fields.md" `
  --crawl `
  --concurrency 24 `
  --timeout 20 `
  --retries 1 `
  --cache ".\data\url_metadata_cache.json" `
  --retry-failures `
  --browser-fallback `
  --browser-fallback-limit 0
```

主要行为：

- 提取 `question`、正确答案、evaluation、信源网址及截图；
- 普通信源先使用异步 HTTP 抓取；
- HTTP 失败或内容不足时使用 Crawl4AI 浏览器兜底；
- `--browser-fallback-limit 0` 表示浏览器兜底数量不设上限；
- Wikipedia 链接不进行 URL 校验，直接标记为“维基百科”；
- URL 抓取结果保存到 `data\url_metadata_cache.json`。

## 2.2 第二阶段：全量图片 OCR

执行 `ocr_and_write.py`，默认等价于：

```powershell
python .\ocr_and_write.py `
  ".\data\新任务25条测试_qc_fields.md" `
  -o ".\data\新任务25条测试_qc_fields_ocr.md" `
  --image-dir ".\data\新任务25条测试_qc_fields_images" `
  --cache ".\data\新任务25条测试_qc_fields_ocr_cache.json" `
  --endpoint "https://www.evern.ccwu.cc/ocr" `
  --limit 0 `
  --download-workers 12 `
  --download-timeout 30 `
  --ocr-timeout 120 `
  --ocr-delay 5 `
  --ocr-retries 8 `
  --ocr-retry-base-delay 15 `
  --ocr-retry-max-delay 300
```

主要行为：

- 下载 Markdown 中全部唯一图片；
- `--limit 0` 表示 OCR 全部图片；
- ?? OCR ?? 自动识别中文、英文和中英文混排；
- 删除输出 Markdown 中的图片链接；
- 图片位置替换成 OCR 文字；
- 复用已下载图片和模式一致的成功 OCR 缓存。

## 2.3 第三阶段：LLM 质检和最终 CSV

执行 `llm_judge.py`，默认等价于：

```powershell
python .\llm_judge.py `
  ".\data\新任务25条测试_qc_fields_ocr.md" `
  -o ".\data\新任务25条测试_qc_fields_ocr_judge.jsonl" `
  --summary ".\data\新任务25条测试_qc_fields_ocr_judge_summary.md" `
  --csv ".\新任务25条测试_qc_result.csv" `
  --model "deepseek-v4-pro-202606" `
  --base-url "https://tokenhub.tencentmaas.com/v1" `
  --workers 3 `
  --timeout 240 `
  --max-tokens 6000 `
  --temperature 0 `
  --retries 2
```

主要行为：

- 读取 `prompt.py` 中的质检 Prompt；
- 按 `## 序号. instance_id` 切分记录；
- 分别检查 answer、信源覆盖和 evaluation；
- 校验模型返回的两个 XML；
- 增量写入 JSONL，用于断点续跑；
- 生成 Markdown 汇总；
- 在主目录生成最终 CSV。

## 2.4 目录结构

完整运行后的典型结构：

```text
自动质检\
├─ .env
├─ .env.example
├─ run_all.py
├─ extract_qc_to_md.py
├─ ocr_and_write.py
├─ llm_judge.py
├─ prompt.py
├─ 新任务25条测试.jsonl
├─ 新任务25条测试_qc_result.csv        # 最终结果
└─ data\                              # 全部中间产物
   ├─ 新任务25条测试_qc_fields.md
   ├─ url_metadata_cache.json
   ├─ 新任务25条测试_qc_fields_ocr.md
   ├─ 新任务25条测试_qc_fields_images\
   ├─ 新任务25条测试_qc_fields_ocr_cache.json
   ├─ 新任务25条测试_qc_fields_ocr_judge.jsonl
   └─ 新任务25条测试_qc_fields_ocr_judge_summary.md
```

## 2.5 常用命令

### 指定最终 CSV 名称

```powershell
python .\run_all.py `
  ".\新任务25条测试.jsonl" `
  -o ".\新任务25条测试_最终质检.csv"
```

### 指定中间目录

```powershell
python .\run_all.py `
  ".\新任务25条测试.jsonl" `
  --data-dir ".\data_0722"
```

### 只查看将要执行的命令

不会调用网络、OCR 或 LLM：

```powershell
python .\run_all.py `
  ".\新任务25条测试.jsonl" `
  --dry-run
```

### 强制刷新全部普通网址

```powershell
python .\run_all.py `
  ".\新任务25条测试.jsonl" `
  --refresh-crawl
```

### 强制重新 OCR

```powershell
python .\run_all.py `
  ".\新任务25条测试.jsonl" `
  --refresh-ocr
```

该参数会忽略成功 OCR 缓存，可能重新产生费用。

### 强制重新 LLM 打标

```powershell
python .\run_all.py `
  ".\新任务25条测试.jsonl" `
  --no-resume-judge
```

## 2.6 断点续跑

默认情况下：

- 信源抓取复用 URL 缓存，只重试历史失败网址；
- OCR 复用本地图片和成功缓存；
- LLM 跳过 JSONL 中已经成功的 `instance_id`；
- 最终 CSV 始终根据 JSONL 中每个 `instance_id` 的最新结果生成。

## 2.7 `run_all.py` 参数速查

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `-o, --output` | `输入名_qc_result.csv` | 最终 CSV 路径 |
| `--data-dir` | `data` | 所有中间产物目录 |
| `--env-file` | 同目录 `.env` | 配置文件 |
| `--crawl-concurrency` | `24` | HTTP 抓取并发数 |
| `--crawl-timeout` | `20` | 网页抓取超时秒数 |
| `--crawl-retries` | `1` | 网页抓取重试次数 |
| `--ocr-download-workers` | `12` | 图片下载并发数 |
| `--ocr-download-timeout` | `30` | 图片下载超时秒数 |
| `--ocr-timeout` | `120` | 单次 OCR 超时秒数 |
| `--ocr-delay` | `5` | Minimum delay between OCR calls |
| `--ocr-retries` | `8` | Retries for 429, 5xx, SSL, and connection errors |
| `--ocr-retry-base-delay` | `15` | Initial retry delay in seconds |
| `--ocr-retry-max-delay` | `300` | Maximum retry delay in seconds |
| `--judge-workers` | `3` | LLM 并发数 |
| `--judge-timeout` | `240` | 单次 LLM 请求超时秒数 |
| `--judge-max-tokens` | `6000` | LLM 最大输出 token |
| `--judge-retries` | `2` | LLM/API/XML 失败重试次数 |
| `--model` | `.env` 中的模型 | 覆盖 `LLM_MODEL` |
| `--base-url` | `.env` 中的地址 | 覆盖 `LLM_BASE_URL` |
| `--refresh-crawl` | 关闭 | 重新抓取全部普通网址 |
| `--refresh-ocr` | 关闭 | 重新调用全部 OCR |
| `--no-resume-judge` | 关闭 | 强制重新 LLM 打标 |
| `--dry-run` | 关闭 | 只打印命令，不执行 |

---

# 3. `extract_qc_to_md.py`：字段提取与信源抓取

该脚本负责把标注平台 JSONL 转成结构化 Markdown。

## 3.1 输入和输出

输入：

```text
新任务25条测试.jsonl
```

默认输出：

```text
新任务25条测试_qc_fields.md
```

只提取字段：

```powershell
python .\extract_qc_to_md.py ".\新任务25条测试.jsonl"
```

抓取信源信息：

```powershell
python .\extract_qc_to_md.py `
  ".\新任务25条测试.jsonl" `
  --crawl
```

## 3.2 提取内容

每条记录包含：

- `question`
- `正确answer`
- `正确eval`
- `答案信源网址及截图`
- dataset/detail/instance 等标识字段
- 标注人
- 可选的信源网站简介

## 3.3 URL 处理

脚本会：

- 排除 Markdown 图片中的截图 URL；
- 提取普通 Markdown 链接和纯文本 URL；
- 清理 URL 末尾多余标点；
- 移除 fragment；
- 对相同 URL 去重；
- 将抓取结果复用到所有引用记录；
- 只读取网页前 1 MB；
- 不下载网页图片。

## 3.4 Wikipedia 规则

所有 `wikipedia.org` 语言子域名都会：

- 跳过 HTTP 校验；
- 跳过 Crawl4AI；
- 不显示状态码和抓取方式；
- 在信源简介中直接显示：

```markdown
#### 1. 维基百科
```

不属于 `wikipedia.org` 的镜像网站仍按普通网站处理。

## 3.5 浏览器兜底

```powershell
python .\extract_qc_to_md.py `
  ".\新任务25条测试.jsonl" `
  --crawl `
  --browser-fallback `
  --browser-fallback-limit 0
```

`0` 表示不限制兜底 URL 数量，可能明显增加运行时间和内存占用。

## 3.6 缓存

默认缓存：

```text
url_metadata_cache.json
```

重新抓取全部普通网址：

```powershell
--refresh
```

只重试缓存中的失败网址：

```powershell
--retry-failures
```

## 3.7 参数速查

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `-o, --output` | `输入名_qc_fields.md` | 输出 Markdown |
| `--crawl` | 关闭 | 抓取信源简介 |
| `--concurrency` | `24` | HTTP 最大并发数 |
| `--timeout` | `20` | 单页面超时秒数 |
| `--retries` | `1` | 重试次数 |
| `--cache` | `url_metadata_cache.json` | URL 缓存 |
| `--refresh` | 关闭 | 刷新全部普通网址 |
| `--retry-failures` | 关闭 | 重试历史失败网址 |
| `--browser-fallback` | 关闭 | 启用 Crawl4AI |
| `--browser-fallback-limit` | `30` | 浏览器兜底上限，`0` 为不限 |

---

# 4. `ocr_and_write.py`：图片下载和中英文 OCR

该脚本负责处理 Markdown 中的图片链接，并生成不含图片链接的 OCR 文档。

## 4.1 `.env`

脚本默认读取同目录 `.env` 中的：

```dotenv
OCR_API_KEY=your_ocr_api_key
OCR_ENDPOINT=https://www.evern.ccwu.cc/ocr
```

已有系统环境变量优先。指定其他配置文件：

```powershell
--env-file ".\config\production.env"
```

## 4.2 默认行为

```powershell
python .\ocr_and_write.py `
  ".\data\新任务25条测试_qc_fields.md"
```

默认生成：

```text
输入名_ocr.md
输入名_images\
输入名_ocr_cache.json
```

处理步骤：

1. 提取并去重所有 Markdown 图片链接；
2. 并发下载全部唯一图片；
3. 调用自建 OCR 服务；
4. 用 OCR 文字替换图片链接；
5. 保存图片和 OCR 缓存。

替换格式：

```text
（此处是图片ocr结果：
识别出的文字
）
```

## 4.3 自建 OCR 接口

默认接口：

```text
https://www.evern.ccwu.cc/ocr
```

请求协议：

```text
Header: x-api-key: <OCR_API_KEY>
Multipart field: file
Response: {"text": "..."}
```

接口直接返回中文、英文和中英文混排结果，不再传递 OCR.Space 的 language 或 engine 参数。 服务端稳定版及部署模板位于 `..\tmp`：`app.py`、`ocr-api.service`、`nginx-ocr.conf`、`server.env.example` 和 `DEPLOY.md`。

可以通过 `.env` 修改接口：

```dotenv
OCR_ENDPOINT=https://www.evern.ccwu.cc/ocr
```

也可以临时使用：

```powershell
--endpoint "https://www.evern.ccwu.cc/ocr"
```
## 4.4 少量测试

OCR 可能产生费用，建议先测试 3～4 张：

```powershell
python .\ocr_and_write.py `
  ".\data\新任务25条测试_qc_fields.md" `
  --limit 4 `
  -o ".\data\新任务25条测试_qc_fields_ocr_test4.md"
```

`--limit` 只限制付费 OCR 数量，全部图片仍会下载。

如果希望保留未处理图片链接：

```powershell
--keep-unprocessed-links
```

## 4.5 缓存规则

缓存会检查：

- 图片 URL；
- OCR 语言；
- OCR 引擎；
- API endpoint。

强制重新 OCR：

```powershell
--refresh-ocr
```

该参数可能重新产生费用。

## 4.6 参数速查

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `--env-file` | 同目录 `.env` | 配置文件 |
| `-o, --output` | `输入名_ocr.md` | 输出 Markdown |
| `--image-dir` | `输入名_images` | 图片目录 |
| `--cache` | `输入名_ocr_cache.json` | OCR 缓存 |
| `--endpoint` | ?? OCR ?? API | OCR 接口地址 |
| `--limit` | `0` | OCR 图片数量，`0` 为全部 |
| `--download-timeout` | `30` | 图片下载超时秒数 |
| `--download-workers` | `12` | 图片下载并发数 |
| `--ocr-timeout` | `120` | OCR 请求超时秒数 |
| `--ocr-delay` | `5` | Minimum delay between OCR calls |
| `--ocr-retries` | `8` | Retries for 429, 5xx, SSL, and connection errors |
| `--ocr-retry-base-delay` | `15` | Initial retry delay in seconds |
| `--ocr-retry-max-delay` | `300` | Maximum retry delay in seconds |
| `--overwrite-images` | 关闭 | 强制重新下载图片 |
| `--refresh-ocr` | 关闭 | 忽略成功 OCR 缓存 |
| `--keep-unprocessed-links` | 关闭 | 保留未 OCR 的图片链接 |

---

# 5. `llm_judge.py`：Prompt 质检与 CSV 打标

该脚本读取 OCR 后的 Markdown，使用 `prompt.py` 逐条质检。

## 5.1 `.env`

默认读取：

```dotenv
LLM_API_KEY=your_llm_api_key
LLM_BASE_URL=https://tokenhub.tencentmaas.com/v1
LLM_MODEL=deepseek-v4-pro-202606
```

已有系统环境变量优先。也可以通过 `--model`、`--base-url` 或 `--env-file` 覆盖。

## 5.2 记录切分

输入 Markdown 必须包含：

```markdown
## 1. ws_en_xxx
## 2. ws_en_xxx
```

脚本按这些标题切分记录，每条记录单独请求模型。

只检查切分，不调用 API：

```powershell
python .\llm_judge.py `
  ".\data\新任务25条测试_qc_fields_ocr.md" `
  --dry-run
```

## 5.3 Prompt 使用方式

- `prompt.py` 中的 `QUALITY_CHECK_PROMPT` 作为 system prompt；
- 单条完整记录作为 user message；
- 网页和 OCR 内容被声明为待审核数据，不能覆盖 system prompt；
- 模型必须输出固定的两个 XML。

## 5.4 XML 校验

必须包含：

```xml
<analysis_process>
  ...
</analysis_process>

<quality_result>
  ...
</quality_result>
```

脚本会检查：

- XML 块数量；
- XML 外是否存在多余文本；
- `PASS/FAIL` 与布尔值是否一致；
- answer 和 evaluation 子结果是否与总体结果一致；
- issue 严重度和所属区域是否合法；
- 修正版 evaluation 是否为完整合法 JSON。

不符合要求时会自动重试。

## 5.5 单条测试

```powershell
python .\llm_judge.py `
  ".\data\新任务25条测试_qc_fields_ocr.md" `
  --instance-id "ws_en_2862_2607131802_45f21e" `
  -o ".\data\judge_test.jsonl" `
  --summary ".\data\judge_test.md" `
  --csv ".\judge_test.csv" `
  --workers 1
```

## 5.6 全量打标

```powershell
python .\llm_judge.py `
  ".\data\新任务25条测试_qc_fields_ocr.md" `
  -o ".\data\新任务25条测试_qc_fields_ocr_judge.jsonl" `
  --summary ".\data\新任务25条测试_qc_fields_ocr_judge_summary.md" `
  --csv ".\新任务25条测试_qc_result.csv" `
  --workers 3 `
  --timeout 240 `
  --max-tokens 6000 `
  --temperature 0 `
  --retries 2
```

## 5.7 输出

### JSONL

机器处理和断点续跑的主记录。每条完成后立即追加写入。

### Markdown 汇总

包含 PASS/FAIL 数量、answer/evaluation 子结果、摘要和完整 XML。

### 最终 CSV

使用 UTF-8 BOM，便于 Excel 直接打开。每个 `instance_id` 保留 JSONL 中最新的一条结果。

主要字段：

- `instance_id`
- `result`
- `is_qualified`
- `answer_qualified`
- `evaluation_qualified`
- `summary`
- `issues`
- `corrected_evaluation`
- `analysis_xml`
- `quality_result_xml`
- token 用量、耗时、重试次数和错误信息

## 5.8 断点续跑

默认跳过 JSONL 中已经成功的 `instance_id`。

强制重新处理：

```powershell
--no-resume
```

失败记录不会被视为完成，下次运行会再次处理。

## 5.9 参数速查

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `--env-file` | 同目录 `.env` | 配置文件 |
| `-o, --output` | `输入名_judge.jsonl` | 增量 JSONL |
| `--summary` | `输入名_judge_summary.md` | Markdown 汇总 |
| `--csv` | 不生成 | 最终 CSV 路径 |
| `--model` | `.env` 或内置值 | 模型名称 |
| `--base-url` | `.env` 或内置值 | OpenAI 兼容接口 |
| `--api-key-env` | `LLM_API_KEY` | API Key 环境变量名 |
| `--workers` | `1` | LLM 并发数 |
| `--limit` | `0` | 处理记录数，`0` 为全部 |
| `--instance-id` | 空 | 只处理指定记录，可重复 |
| `--timeout` | `180` | 单请求超时秒数 |
| `--max-tokens` | `6000` | 最大输出 token |
| `--temperature` | `0` | 采样温度 |
| `--retries` | `2` | API/XML 失败重试次数 |
| `--no-resume` | 关闭 | 重新处理已有成功记录 |
| `--dry-run` | 关闭 | 只检查记录切分 |

---

# 6. `prompt.py`：质检标准

`prompt.py` 导出：

```python
pe
QUALITY_CHECK_PROMPT
```

两者内容相同，保留 `pe` 是为了兼容旧调用。

Prompt 主要检查：

## 6.1 answer

- Markdown 格式和表格完整性；
- 是否完整回答 question；
- 每个关键结论是否有信源覆盖；
- 信源是否属于 A/B 类；
- 是否存在无支撑推断、C/D 类核心信源或证据错配。

## 6.2 evaluation

- JSON 是否合法；
- `required` 是否与题目和答案列一致；
- `unique_columns` 是否真正唯一且语义稳定；
- `eval_pipeline` 是否逐列对应；
- preprocess、metric 和 criterion 是否合理；
- `exact_match` 是否过严；
- `llm_judge` criterion 是否具体；
- `number_near` 容差是否合理。

## 6.3 固定输出

Prompt 要求只输出两个 XML：

```xml
<analysis_process>...</analysis_process>
<quality_result>...</quality_result>
```

如果 evaluation 需要修改，必须在 `corrected_evaluation` 中返回完整、合法、可直接使用的 JSON。

---

# 7. 常见问题

## OCR 429, SSL EOF, or aborted connections

These normally indicate API rate limiting or a transient network failure, not necessarily an oversized image. The script now:

- waits between normal OCR calls;
- retries HTTP 429, HTTP 5xx, SSL EOF, connection aborts, and timeouts;
- honors `Retry-After` when available, otherwise uses exponential backoff;
- saves the cache immediately after each image.

Rerun the original command to resume. Successful images are read from cache. If 429 errors remain frequent, slow it down further:

```powershell
python .\run_all.py ".\new_task.jsonl" `
  --ocr-delay 4 `
  --ocr-retries 10 `
  --ocr-retry-base-delay 20 `
  --ocr-retry-max-delay 300
```

Do not add `--refresh-ocr`, or successful cached images will be called again.

## 缺少 OCR 或 LLM Key

检查 `.env`：

```dotenv
OCR_API_KEY=...
LLM_API_KEY=...
```

单独运行脚本时也会默认加载同目录 `.env`。

## 为什么 `--limit 4` 仍下载全部图片？

`--limit` 只限制付费 OCR 调用数量，不限制图片下载，以便后续全量运行直接复用图片。

## 为什么 Wikipedia 没有状态码和描述？

Wikipedia 被配置为跳过 URL 校验，直接标记为“维基百科”。

## 为什么重新运行没有再次调用 OCR 或 LLM？

默认启用断点续跑：

- OCR 复用成功缓存；
- LLM 跳过已经成功的 `instance_id`。

如需强制刷新，分别使用：

```text
--refresh-ocr
--no-resume
```

## 为什么最终 CSV 中包含 XML？

XML 是完整的可审计质检结果。CSV 同时提供了拆分后的 PASS/FAIL、summary 和 issues，常规筛选时不需要解析 XML。

## 如何查看完整参数？

```powershell
python .\run_all.py --help
python .\extract_qc_to_md.py --help
python .\ocr_and_write.py --help
python .\llm_judge.py --help
```

# cd ocr && source venv_ocr_clean/bin/activate 

nohup uvicorn app:app \
--host 0.0.0.0 \
--port 8000 \
--workers 1 \
--timeout-keep-alive 300 \
> ocr.log 2>&1 &