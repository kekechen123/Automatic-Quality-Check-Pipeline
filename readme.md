## 使用
```
python .\extract_qc_to_md.py "新任务25条测试.jsonl" `
--crawl `
--browser-fallback `
--browser-fallback-limit 0
```
## 已更新文件

- 脚本：[extract_qc_to_md.py](D:\每日工作\rlwide search长期\extract_qc_to_md.py)
- 示例输出：[新任务25条测试_qc_fields.md](D:\每日工作\rlwide search长期\新任务25条测试_qc_fields.md)
- 网站信息缓存：[url_metadata_cache.json](D:\每日工作\rlwide search长期\url_metadata_cache.json)

## 抓取的信息

每个信源网址新增以下内容：

- URL
- 跳转后的最终 URL
- HTTP 状态码
- 网页标题
- 网页描述
- 网页关键词
- 抓取方式：http 或 browser
- 抓取错误信息

Markdown 中会在每条数据后新增：

### 信源网站简介

#### 1. 页面标题

- **URL**: https://example.com/page
- **Status**: 200
- **Method**: http
- **Description**: 网站或页面的简要描述
- **Keywords**: ...

## 性能策略

没有直接对所有网站都启动 Playwright 浏览器，因为这种方式对于大量 URL 很慢，也容易被少数卡死页面拖住。

当前采用两级抓取：

### 第一层：异步 HTTP 批量抓取

使用 aiohttp：

- 默认并发：24
- 同一域名最多同时请求2个
- DNS结果缓存
- 连接复用
- 最多读取页面前1MB
- 只解析标题、描述、关键词
- 不下载图片
- 不执行JavaScript
- 支持超时与失败重试
- 每完成20个网址保存一次缓存

### 第二层：可选 Crawl4AI 浏览器兜底

对于以下页面，可以启用 Crawl4AI：

- HTTP返回403
- 必须执行JavaScript
- HTTP方式没有拿到标题和描述
- 普通请求被反爬拦截

浏览器兜底会：

- 使用较低并发，避免内存爆炸
- 分小批次运行
- 默认最多处理30个失败网址
- 每批完成后写入缓存
- 使用轻量、文本、无截图模式

## URL 处理

脚本会自动：

- 排除 ![image](...) 中的截图链接
- 提取普通 Markdown 链接
- 提取纯文本 URL
- 修复链接末尾多余标点
- 移除 URL fragment
- 全文件范围去重
- 同一个网址只爬一次
- 将结果回填到引用它的所有记录中

25条样例中：

- 原始链接包含大量截图地址
- 过滤截图后得到96个唯一信源网页
- 96个网址首次HTTP抓取约 17.4秒
- 有效标题：90个
- 有效描述：69个
- 缓存后再次生成Markdown约 0.4秒

## 推荐运行方式

### 快速模式

适合批量处理，优先推荐：

python .\extract_qc_to_md.py "新任务25条测试.jsonl" --crawl

默认参数相当于：

python .\extract_qc_to_md.py "新任务25条测试.jsonl" `
  --crawl `
  --concurrency 24 `
  --timeout 20 `
  --retries 1

### 开启浏览器兜底

python .\extract_qc_to_md.py "新任务25条测试.jsonl" `
  --crawl `
  --browser-fallback

这会先进行快速HTTP抓取，再把失败或缺少简介的网站交给 Crawl4AI。

默认最多兜底30个网址，防止大量403网站导致浏览器运行太久：

设置为 0 表示不限制：

--browser-fallback-limit 0

对于几百或几千个不同网站，不建议设置为0。

### 重新抓取全部网址

默认会直接使用已有缓存。如需强制刷新：

python .\extract_qc_to_md.py "新任务25条测试.jsonl" `
  --crawl `
  --refresh

### 只重试之前失败的网址

python .\extract_qc_to_md.py "新任务25条测试.jsonl" `
  --crawl `
  --retry-failures `
  --browser-fallback

## 缓存机制

缓存默认保存在：

url_metadata_cache.json

处理其他JSONL时，相同网址不需要重复抓取。因此，多个批次持续共用这份缓存时，后续速度会越来越快。