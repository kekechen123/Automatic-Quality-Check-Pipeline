# 自动质检工具说明

本目录包含两个脚本：

| 脚本 | 输入 | 输出 | 作用 |
|---|---|---|---|
| `extract_qc_to_md.py` | 标注平台 JSONL | `*_qc_fields.md` | 提取质检字段，并可抓取普通信源网页简介 |
| `ocr_and_write.py` | 含截图的 Markdown | `*_ocr.md` | 下载图片、自动识别中英文，并用 OCR 文字替换图片链接 |

推荐流程：

```text
JSONL → extract_qc_to_md.py → *_qc_fields.md → ocr_and_write.py → *_ocr.md
```

---

## 1. 环境准备

建议使用 Python 3.10 或更高版本。

### 基础依赖

```powershell
pip install aiohttp beautifulsoup4 lxml requests
```

如需启用浏览器兜底抓取，再安装：

```powershell
pip install crawl4ai
```

### OCR API Key

`ocr_and_write.py` 默认调用 OCR.Space。运行前设置：

```powershell
$env:OCR_API_KEY="你的 OCR API Key"
```

脚本只从环境变量读取 Key，不会把 Key 写入 Markdown、缓存或日志。

---

## 2. 提取质检字段：`extract_qc_to_md.py`

### 只提取字段

```powershell
python .\extract_qc_to_md.py ".\新任务25条测试.jsonl"
```

默认输出：

```text
新任务25条测试_qc_fields.md
```

### 提取字段并抓取信源网页

推荐命令：

```powershell
python .\extract_qc_to_md.py `
  ".\新任务25条测试.jsonl" `
  --crawl
```

默认抓取参数：

- HTTP 总并发：24
- 单页面超时：20 秒
- 失败重试：1 次
- URL 缓存：`url_metadata_cache.json`

完整写法：

```powershell
python .\extract_qc_to_md.py `
  ".\新任务25条测试.jsonl" `
  --crawl `
  --concurrency 24 `
  --timeout 20 `
  --retries 1
```

指定输出文件：

```powershell
python .\extract_qc_to_md.py `
  ".\新任务25条测试.jsonl" `
  --crawl `
  -o ".\质检结果.md"
```

### Wikipedia 特殊规则

所有 `wikipedia.org` 及其语言子域名，例如：

```text
https://en.wikipedia.org/...
https://zh.wikipedia.org/...
```

都会：

1. 跳过 HTTP URL 校验；
2. 跳过 Crawl4AI；
3. 不显示状态码、抓取方式和网页描述；
4. 在“信源网站简介”中直接标记：

```markdown
#### 1. 维基百科
```

名称中虽然含有 `wikipedia`、但域名不属于 `wikipedia.org` 的镜像站，仍按普通网站处理。

### URL 处理规则

脚本会自动：

- 排除 `![image](...)` 中的截图 URL；
- 提取普通 Markdown 链接和纯文本 URL；
- 清理 URL 末尾多余标点；
- 移除 URL fragment；
- 对相同 URL 去重；
- 将抓取结果复用到所有引用记录；
- 只读取网页前 1 MB；
- 不下载网页中的图片。

### 浏览器兜底

普通 HTTP 抓取失败、内容不完整或页面依赖 JavaScript 时，可以启用 Crawl4AI：

```powershell
python .\extract_qc_to_md.py `
  ".\新任务25条测试.jsonl" `
  --crawl `
  --browser-fallback
```

默认最多处理 30 个失败 URL：

```powershell
--browser-fallback-limit 30
```

设置为 `0` 表示不限制。大量网址时不建议使用无限制模式。

### 缓存与重新抓取

强制刷新全部普通网址：

```powershell
python .\extract_qc_to_md.py `
  ".\新任务25条测试.jsonl" `
  --crawl `
  --refresh
```

只重试缓存中的失败网址：

```powershell
python .\extract_qc_to_md.py `
  ".\新任务25条测试.jsonl" `
  --crawl `
  --retry-failures `
  --browser-fallback
```

自定义缓存位置：

```powershell
--cache ".\my_url_cache.json"
```

### 参数速查

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `-o, --output` | `输入名_qc_fields.md` | 输出 Markdown |
| `--crawl` | 关闭 | 抓取普通信源网页简介 |
| `--concurrency` | `24` | HTTP 最大并发数 |
| `--timeout` | `20` | 单页面超时，单位为秒 |
| `--retries` | `1` | 临时错误重试次数 |
| `--cache` | `url_metadata_cache.json` | URL 元数据缓存 |
| `--refresh` | 关闭 | 忽略缓存并重新抓取全部普通网址 |
| `--retry-failures` | 关闭 | 只重试缓存中的失败网址 |
| `--browser-fallback` | 关闭 | 为失败页面启用 Crawl4AI |
| `--browser-fallback-limit` | `30` | 浏览器兜底 URL 上限，`0` 为不限制 |

---

## 3. 图片 OCR：`ocr_and_write.py`

### 默认行为

脚本会：

1. 扫描 Markdown 中全部 `![...](...)` 图片；
2. 对相同图片 URL 去重；
3. 并发下载全部唯一图片；
4. 使用 OCR.Space Engine 3 自动识别中英文；
5. 将 OCR 结果写入新 Markdown；
6. 删除输出文档中的图片链接；
7. 缓存 OCR 结果，避免重复付费。

图片链接会被替换为：

```text
（此处是图片ocr结果：
识别出的文字
）
```

### 推荐运行方式

```powershell
$env:OCR_API_KEY="你的 OCR API Key"

python .\ocr_and_write.py `
  ".\新任务25条测试_qc_fields.md"
```

默认生成：

```text
新任务25条测试_qc_fields_ocr.md
新任务25条测试_qc_fields_images\
新任务25条测试_qc_fields_ocr_cache.json
```

- `*_ocr.md`：图片已替换成 OCR 文字的新文档；
- `*_images\`：下载的原始图片；
- `*_ocr_cache.json`：OCR 结果缓存。

### 中英文自动识别

默认配置：

```text
--language auto
--engine 3
```

该模式可在一次请求中识别中文、英文和中英文混排，不需要分别调用中文、英文 OCR。

也可以强制指定语言：

```powershell
--language eng
--language chs
```

`--language auto` 必须配合 `--engine 3`，脚本会检查错误组合。

### 先测试少量图片

OCR 可能产生费用，建议先测试 3～4 张：

```powershell
python .\ocr_and_write.py `
  ".\新任务25条测试_qc_fields.md" `
  --limit 4 `
  -o ".\新任务25条测试_qc_fields_ocr_test4.md"
```

使用 `--limit 4` 时：

- 全部图片仍会下载到本地；
- 只对前 4 张唯一图片调用 OCR；
- 默认移除其他图片链接，并写入“未执行 OCR”提示；
- 后续运行全量 OCR 时可以复用已下载图片。

如果希望测试文档保留未处理的图片链接：

```powershell
--keep-unprocessed-links
```

### 全量 OCR

确认测试效果后，不传 `--limit`：

```powershell
python .\ocr_and_write.py `
  ".\新任务25条测试_qc_fields.md" `
  -o ".\新任务25条测试_qc_fields_ocr.md"
```

已下载图片和识别模式相同的成功缓存会自动复用。

### 指定输出位置

```powershell
python .\ocr_and_write.py `
  ".\新任务25条测试_qc_fields.md" `
  -o ".\输出\质检_OCR.md" `
  --image-dir ".\输出\images" `
  --cache ".\输出\ocr_cache.json"
```

### OCR 缓存规则

缓存会检查：

- 图片 URL；
- `language`；
- `engine`；
- OCR API endpoint。

例如，之前使用 `eng + Engine 2` 得到的缓存，不会被错误复用到 `auto + Engine 3`。

强制重新识别：

```powershell
--refresh-ocr
```

该参数会忽略成功缓存，可能产生额外费用，请谨慎使用。

### 下载参数

默认下载并发为 12：

```powershell
--download-workers 12
```

网络不稳定时可以降低：

```powershell
--download-workers 4
```

调整下载和 OCR 超时：

```powershell
--download-timeout 60
--ocr-timeout 180
```

强制重新下载已有图片：

```powershell
--overwrite-images
```

### 参数速查

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `-o, --output` | `输入名_ocr.md` | OCR 输出 Markdown |
| `--image-dir` | `输入名_images` | 图片下载目录 |
| `--cache` | `输入名_ocr_cache.json` | OCR 缓存文件 |
| `--endpoint` | OCR.Space API | 自定义 OCR API 地址 |
| `--language` | `auto` | `auto`、`eng` 或 `chs` |
| `--engine` | `3` | OCR.Space 引擎；自动语言要求 Engine 3 |
| `--limit` | `0` | 最多 OCR 的唯一图片数；`0` 为全部 |
| `--download-timeout` | `30` | 单张图片下载超时，单位为秒 |
| `--download-workers` | `12` | 图片下载并发数 |
| `--ocr-timeout` | `120` | 单次 OCR 请求超时，单位为秒 |
| `--overwrite-images` | 关闭 | 强制重新下载已有图片 |
| `--refresh-ocr` | 关闭 | 忽略成功缓存并重新识别 |
| `--keep-unprocessed-links` | 关闭 | 使用 `--limit` 时保留未处理图片链接 |

---

## 4. 完整推荐流程

### 第一步：提取字段和普通信源信息

```powershell
python .\extract_qc_to_md.py `
  ".\新任务25条测试.jsonl" `
  --crawl
```

### 第二步：测试 4 张图片

```powershell
$env:OCR_API_KEY="你的 OCR API Key"

python .\ocr_and_write.py `
  ".\新任务25条测试_qc_fields.md" `
  --limit 4 `
  -o ".\新任务25条测试_qc_fields_ocr_test4.md"
```

### 第三步：确认效果后运行全量 OCR

```powershell
python .\ocr_and_write.py `
  ".\新任务25条测试_qc_fields.md" `
  -o ".\新任务25条测试_qc_fields_ocr.md"
```

---

## 5. 已完成的测试

已对记录 `ws_en_2862_2607131802_45f21e` 进行测试：

- 图片：3 张；
- 下载成功：3/3；
- OCR 成功：3/3；
- 模式：`auto + Engine 3`；
- 输出中剩余图片链接：0；
- 中文、英文和中英文混排均可识别。

测试输出：

```text
ws_en_2862_2607131802_45f21e_ocr.md
```

---

## 6. 常见问题

### 提示缺少 `OCR_API_KEY`

当前 PowerShell 会话尚未设置 Key：

```powershell
$env:OCR_API_KEY="你的 OCR API Key"
```

关闭 PowerShell 后，临时环境变量通常需要重新设置。

### 为什么 `--limit 4` 仍下载全部图片？

`--limit` 只限制付费 OCR 次数，不限制图片下载。这样后续全量运行时不需要重新下载。

### 为什么全量运行又识别了测试过的图片？

只有图片 URL、语言、OCR 引擎和 endpoint 全部一致时才会复用缓存。从英文模式切换到自动模式后，旧缓存不会复用。

### 为什么输出 Markdown 中没有图片？

这是脚本的默认设计：图片链接会被 OCR 文字替换。使用 `--limit` 时，如需保留未处理图片，请添加：

```powershell
--keep-unprocessed-links
```

### Wikipedia 为什么没有状态码和描述？

Wikipedia 被配置为跳过 URL 校验，直接标记为“维基百科”。

### 如何查看完整参数？

```powershell
python .\extract_qc_to_md.py --help
python .\ocr_and_write.py --help
```