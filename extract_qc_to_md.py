#!/usr/bin/env python3
"""Extract QC fields from annotation-platform JSONL and render Markdown."""

from __future__ import annotations

import argparse
import asyncio
import html as html_lib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

LABEL_ANSWER = "\u6b63\u786eanswer"
LABEL_EVAL = "\u6b63\u786eeval"
LABEL_SOURCE = "\u7b54\u6848\u4fe1\u6e90\u7f51\u5740\u53ca\u622a\u56fe"
TARGET_LABELS = (LABEL_ANSWER, LABEL_EVAL, LABEL_SOURCE)
EMPTY = "\uff08\u7a7a\uff09"
URL_INFO_KEY = "url_info"
URL_RE = re.compile(r"https?://[^\s<>\u3000-\u9fff]+", re.IGNORECASE)
MARKDOWN_LINK_RE = re.compile(
    r"(!?)\[[^\]]*\]\((https?://(?:[^()\s]+|\([^()\s]*\))*)\)",
    re.IGNORECASE,
)
TRAILING_URL_CHARS = ".,;:!?\"'\u3002\uff0c\uff1b\uff1a\uff01\uff1f\u3001"
WIKIPEDIA_HOST_RE = re.compile(r"(?:^|\.)wikipedia\.org$", re.IGNORECASE)


def decode_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def normalize_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\u3001".join(normalize_value(item) for item in value)
    if isinstance(value, (dict, tuple)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def extract_label_values(detail_label: Any) -> dict[str, str]:
    detail = decode_json_object(detail_label)
    result = {label: "" for label in TARGET_LABELS}
    for tag in detail.get("tags") or []:
        if not isinstance(tag, dict):
            continue
        label = tag.get("label")
        if label in result:
            result[label] = normalize_value(tag.get("value"))
    return result


def extract_record(raw: dict[str, Any]) -> dict[str, str]:
    content = decode_json_object(raw.get("datasetItemContent"))
    labels = extract_label_values(raw.get("detailLabel"))
    return {
        "question": normalize_value(content.get("question") or raw.get("question")),
        "datasetItemId": normalize_value(raw.get("datasetItemId")),
        "detailId": normalize_value(raw.get("detailId")),
        "instance_id": normalize_value(
            content.get("instance_id")
            or content.get("nstance_id")
            or raw.get("instance_id")
            or raw.get("nstance_id")
        ),
        LABEL_ANSWER: labels[LABEL_ANSWER],
        LABEL_EVAL: labels[LABEL_EVAL],
        LABEL_SOURCE: labels[LABEL_SOURCE],
        "labeler": normalize_value(raw.get("labeler")),
    }



def clean_url(url: str) -> str:
    url = html_lib.unescape(url.strip()).rstrip(TRAILING_URL_CHARS)
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1]
    return url


def normalize_url(url: str) -> str:
    url = clean_url(url)
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        if scheme not in ("http", "https") or not host:
            return ""
        port = parts.port
        netloc = host
        if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            netloc = f"{host}:{port}"
        path = parts.path or "/"
        return urlunsplit((scheme, netloc, path, parts.query, ""))
    except ValueError:
        return url


def is_wikipedia_url(url: str) -> bool:
    """Return True for any language/subdomain under wikipedia.org."""
    try:
        host = (urlsplit(clean_url(url)).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return bool(WIKIPEDIA_HOST_RE.search(host))


def wikipedia_url_info(url: str) -> dict[str, Any]:
    """Create a fixed display entry without issuing an HTTP request."""
    return {
        "url": url, "final_url": url, "success": True, "status_code": None,
        "title": "\u7ef4\u57fa\u767e\u79d1", "description": "", "keywords": "", "error": "",
        "method": "skip-wikipedia", "source_type": "wikipedia",
    }


def extract_source_urls(text: str) -> list[str]:
    """Extract source page URLs, excluding Markdown image/screenshot targets."""
    if not text:
        return []
    urls: list[str] = []
    seen: set[str] = set()
    occupied: list[tuple[int, int]] = []

    def add(url: str) -> None:
        url = normalize_url(url)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)

    for match in MARKDOWN_LINK_RE.finditer(text):
        occupied.append(match.span())
        if match.group(1) != "!":
            add(match.group(2))

    for match in URL_RE.finditer(text):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        add(match.group(0))
    return urls


def load_url_cache(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_url_cache(path: Path, cache: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def first_text(meta: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


async def crawl_url_infos(
    urls: list[str],
    cache_path: Path,
    concurrency: int,
    timeout_ms: int,
    retries: int,
    refresh: bool,
    retry_failures: bool,
    browser_fallback: bool = False,
    browser_fallback_limit: int = 30,
) -> dict[str, dict[str, Any]]:
    """Fetch metadata with high-throughput HTTP; optionally use Crawl4AI as fallback."""
    cache = load_url_cache(cache_path)
    pending = []
    for url in urls:
        if is_wikipedia_url(url):
            continue
        cached = cache.get(url)
        if refresh or cached is None or (retry_failures and not cached.get("success")):
            pending.append(url)
    if not pending:
        return cache

    try:
        import aiohttp
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise RuntimeError("aiohttp and beautifulsoup4 are required for --crawl") from exc

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8,zh-CN;q=0.6",
    }
    semaphore = asyncio.Semaphore(max(1, concurrency))
    domain_locks: dict[str, asyncio.Semaphore] = {}
    timeout = aiohttp.ClientTimeout(
        total=max(1, timeout_ms) / 1000,
        connect=min(8, max(1, timeout_ms) / 1000),
        sock_read=min(10, max(1, timeout_ms) / 1000),
    )
    connector = aiohttp.TCPConnector(
        limit=max(1, concurrency),
        limit_per_host=2,
        ttl_dns_cache=600,
        enable_cleanup_closed=True,
    )

    def meta_content(soup: Any, *selectors: tuple[str, str]) -> str:
        for attr, value in selectors:
            node = soup.find("meta", attrs={attr: value})
            if node and node.get("content"):
                return " ".join(str(node["content"]).split())
        return ""

    async def fetch_one(session: Any, url: str) -> tuple[str, dict[str, Any]]:
        host = urlsplit(url).hostname or ""
        host_sem = domain_locks.setdefault(host, asyncio.Semaphore(2))
        last_error = ""
        for attempt in range(retries + 1):
            try:
                async with semaphore, host_sem:
                    async with session.get(url, allow_redirects=True) as response:
                        content_type = response.headers.get("Content-Type", "")
                        status = response.status
                        final_url = str(response.url)
                        if "text/html" not in content_type.lower() and "application/xhtml" not in content_type.lower():
                            return url, {
                                "url": url, "final_url": final_url, "success": 200 <= status < 400,
                                "status_code": status, "title": Path(urlsplit(final_url).path).name,
                                "description": f"Non-HTML resource ({content_type or 'unknown content type'})",
                                "keywords": "", "error": "" if 200 <= status < 400 else f"HTTP {status}",
                                "method": "http", "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            }
                        body = await response.content.read(1024 * 1024)
                        encoding = response.charset or "utf-8"
                        try:
                            text = body.decode(encoding, errors="replace")
                        except LookupError:
                            text = body.decode("utf-8", errors="replace")
                        soup = BeautifulSoup(text, "lxml")
                        title = " ".join(soup.title.get_text(" ", strip=True).split()) if soup.title else ""
                        title = meta_content(soup, ("property", "og:title"), ("name", "twitter:title")) or title
                        description = meta_content(
                            soup,
                            ("name", "description"),
                            ("property", "og:description"),
                            ("name", "twitter:description"),
                        )
                        keywords = meta_content(soup, ("name", "keywords"))
                        ok = 200 <= status < 400
                        return url, {
                            "url": url, "final_url": final_url, "success": ok,
                            "status_code": status, "title": title, "description": description,
                            "keywords": keywords, "error": "" if ok else f"HTTP {status}",
                            "method": "http", "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        }
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < retries:
                    await asyncio.sleep(min(2.0, 0.25 * (2 ** attempt)))
        return url, {
            "url": url, "final_url": url, "success": False, "status_code": None,
            "title": "", "description": "", "keywords": "", "error": last_error or "fetch failed",
            "method": "http", "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    print(f"Fetching {len(pending)} unique source URLs with HTTP concurrency={concurrency}...")
    started = time.perf_counter()
    async with aiohttp.ClientSession(headers=headers, timeout=timeout, connector=connector) as session:
        tasks = [asyncio.create_task(fetch_one(session, url)) for url in pending]
        completed = 0
        for task in asyncio.as_completed(tasks):
            url, info = await task
            cache[url] = info
            completed += 1
            if completed % 20 == 0 or completed == len(tasks):
                save_url_cache(cache_path, cache)
                print(f"HTTP progress: {completed}/{len(tasks)}")

    if browser_fallback:
        fallback_urls = [
            url for url in pending
            if not cache[url].get("success") or not (cache[url].get("title") or cache[url].get("description"))
        ]
        if browser_fallback_limit > 0:
            fallback_urls = fallback_urls[:browser_fallback_limit]
        if fallback_urls:
            cache = await crawl4ai_fallback(
                fallback_urls, cache, cache_path,
                concurrency=min(max(1, concurrency // 3), 4),
                timeout_ms=timeout_ms,
                retries=retries,
            )

    save_url_cache(cache_path, cache)
    elapsed = time.perf_counter() - started
    print(f"Fetch finished in {elapsed:.1f}s; cache: {cache_path}")
    return cache


async def crawl4ai_fallback(
    urls: list[str],
    cache: dict[str, dict[str, Any]],
    cache_path: Path,
    concurrency: int,
    timeout_ms: int,
    retries: int,
) -> dict[str, dict[str, Any]]:
    """Low-concurrency browser fallback for JS-only or HTTP-blocked pages."""
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig, SemaphoreDispatcher
    except ImportError:
        return cache
    browser_config = BrowserConfig(
        headless=True, verbose=False, text_mode=True, light_mode=True,
        java_script_enabled=True, memory_saving_mode=True, max_pages_before_recycle=20,
    )
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.ENABLED, wait_until="domcontentloaded", page_timeout=timeout_ms,
        delay_before_return_html=0, wait_for_images=False, scan_full_page=False,
        process_iframes=False, screenshot=False, only_text=True, verbose=False, max_retries=retries,
    )
    crawl4ai_base = Path(tempfile.gettempdir()) / "qc_crawl4ai"
    crawl4ai_base.mkdir(parents=True, exist_ok=True)
    os.environ["CRAWL4_AI_BASE_DIRECTORY"] = str(crawl4ai_base)
    print(f"Browser fallback: {len(urls)} URLs, concurrency={concurrency}...")
    # Small batches ensure one pathological page cannot hold the complete job forever.
    batch_size = max(1, concurrency * 3)
    async with AsyncWebCrawler(config=browser_config, base_directory=str(crawl4ai_base)) as crawler:
        for offset in range(0, len(urls), batch_size):
            batch = urls[offset:offset + batch_size]
            dispatcher = SemaphoreDispatcher(semaphore_count=concurrency, max_session_permit=concurrency)
            try:
                results = await asyncio.wait_for(
                    crawler.arun_many(batch, config=run_config, dispatcher=dispatcher),
                    timeout=(timeout_ms / 1000 + 5) * max(1, len(batch) / concurrency),
                )
                for requested_url, result in zip(batch, results):
                    meta = result.metadata or {}
                    info = {
                        "url": requested_url, "final_url": result.redirected_url or result.url,
                        "success": bool(result.success), "status_code": result.status_code,
                        "title": first_text(meta, "title", "og:title", "twitter:title"),
                        "description": first_text(meta, "description", "og:description", "twitter:description"),
                        "keywords": first_text(meta, "keywords"),
                        "error": "" if result.success else (result.error_message or "crawl failed"),
                        "method": "browser", "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    if info["success"] or info["title"] or info["description"]:
                        cache[requested_url] = info
            except (asyncio.TimeoutError, Exception) as exc:
                for url in batch:
                    cache[url]["browser_error"] = f"{type(exc).__name__}: {exc}"
            save_url_cache(cache_path, cache)
    return cache


def attach_url_infos(records: list[dict[str, Any]], cache: dict[str, dict[str, Any]]) -> None:
    for record in records:
        infos: list[dict[str, Any]] = []
        for url in extract_source_urls(record[LABEL_SOURCE]):
            if is_wikipedia_url(url):
                infos.append(wikipedia_url_info(url))
            else:
                infos.append(cache.get(url, {"url": url, "success": False, "error": "not crawled"}))
        record[URL_INFO_KEY] = infos


def render_url_infos(infos: list[dict[str, Any]]) -> list[str]:
    if not infos:
        return [EMPTY]
    lines: list[str] = []
    for index, info in enumerate(infos, 1):
        url = info.get("url") or ""
        if info.get("source_type") == "wikipedia" or info.get("method") == "skip-wikipedia":
            lines.extend([f"#### {index}. \u7ef4\u57fa\u767e\u79d1", ""])
            continue
        lines.append(f"#### {index}. {info.get('title') or url or EMPTY}")
        lines.append("")
        lines.append(f"- **URL**: {url or EMPTY}")
        if info.get("final_url") and info.get("final_url") != url:
            lines.append(f"- **Final URL**: {info['final_url']}")
        lines.append(f"- **Status**: {info.get('status_code') or EMPTY}")
        if info.get("method"):
            lines.append(f"- **Method**: {info['method']}")
        if info.get("description"):
            lines.append(f"- **Description**: {info['description']}")
        if info.get("keywords"):
            lines.append(f"- **Keywords**: {info['keywords']}")
        if not info.get("success"):
            lines.append(f"- **Error**: {info.get('error') or 'crawl failed'}")
        lines.append("")
    return lines

def fenced(text: str, language: str = "") -> str:
    text = text or EMPTY
    longest = current = 0
    for char in text:
        if char == "`":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    fence = "`" * max(3, longest + 1)
    suffix = "" if text.endswith("\n") else "\n"
    return f"{fence}{language}\n{text}{suffix}{fence}"


def render_markdown(records: list[dict[str, Any]], source: Path, errors: list[str]) -> str:
    lines = [
        "# \u81ea\u52a8\u8d28\u68c0\u5b57\u6bb5\u63d0\u53d6",
        "",
        f"- \u6765\u6e90\u6587\u4ef6\uff1a`{source.name}`",
        f"- \u6210\u529f\u63d0\u53d6\uff1a{len(records)} \u6761",
        f"- \u89e3\u6790\u5931\u8d25\uff1a{len(errors)} \u6761",
        "",
    ]
    for index, record in enumerate(records, 1):
        title_id = record["instance_id"] or record["datasetItemId"] or str(index)
        lines.extend([
            f"## {index}. {title_id}", "",
            f"- **datasetItemId**\uff1a`{record['datasetItemId'] or EMPTY}`",
            f"- **detailId**\uff1a`{record['detailId'] or EMPTY}`",
            f"- **instance_id**\uff1a`{record['instance_id'] or EMPTY}`",
            f"- **labeler**\uff1a{record['labeler'] or EMPTY}", "",
            "### question", "", record["question"] or EMPTY, "",
            f"### {LABEL_ANSWER}", "", fenced(record[LABEL_ANSWER], "markdown"), "",
            f"### {LABEL_EVAL}", "", fenced(record[LABEL_EVAL], "json"), "",
            f"### {LABEL_SOURCE}", "", record[LABEL_SOURCE] or EMPTY, "",
            "### \u4fe1\u6e90\u7f51\u7ad9\u7b80\u4ecb", "",
        ])
        lines.extend(render_url_infos(record.get(URL_INFO_KEY) or []))
        lines.extend(["---", ""])
    if errors:
        lines.extend(["## \u89e3\u6790\u5931\u8d25\u8bb0\u5f55", ""])
        lines.extend(f"- {message}" for message in errors)
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Input JSONL file")
    parser.add_argument("-o", "--output", type=Path, help="Output Markdown path")
    parser.add_argument("--crawl", action="store_true", help="Crawl source URLs and append site summaries")
    parser.add_argument("--concurrency", type=int, default=24, help="Maximum concurrent HTTP requests")
    parser.add_argument("--timeout", type=int, default=20, help="Per-page timeout in seconds")
    parser.add_argument("--retries", type=int, default=1, help="Retries for transient/rate-limit failures")
    parser.add_argument("--cache", type=Path, help="Persistent URL metadata cache JSON")
    parser.add_argument("--refresh", action="store_true", help="Refresh all cached URLs")
    parser.add_argument("--retry-failures", action="store_true", help="Retry cached failed URLs")
    parser.add_argument("--browser-fallback", action="store_true", help="Use Crawl4AI for HTTP failures/JS-only pages")
    parser.add_argument("--browser-fallback-limit", type=int, default=30, help="Maximum URLs sent to browser fallback; 0 means no limit")
    args = parser.parse_args()
    input_path = args.input
    output_path = args.output or input_path.with_name(f"{input_path.stem}_qc_fields.md")
    cache_path = args.cache or input_path.with_name("url_metadata_cache.json")
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        stream = input_path.open("r", encoding="utf-8-sig")
    except OSError as exc:
        print(f"Cannot open input: {exc}", file=sys.stderr)
        return 1
    with stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError("top-level JSON is not an object")
                records.append(extract_record(raw))
            except (json.JSONDecodeError, ValueError) as exc:
                errors.append(f"line {line_number}: {exc}")
    if args.crawl:
        all_urls = list(dict.fromkeys(
            url
            for record in records
            for url in extract_source_urls(record[LABEL_SOURCE])
            if not is_wikipedia_url(url)
        ))
        try:
            cache = asyncio.run(crawl_url_infos(
                all_urls,
                cache_path=cache_path,
                concurrency=max(1, args.concurrency),
                timeout_ms=max(1, args.timeout) * 1000,
                retries=max(0, args.retries),
                refresh=args.refresh,
                retry_failures=args.retry_failures,
                browser_fallback=args.browser_fallback,
                browser_fallback_limit=max(0, args.browser_fallback_limit),
            ))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        attach_url_infos(records, cache)
    output_path.write_text(render_markdown(records, input_path, errors), encoding="utf-8")
    print(f"Output: {output_path}")
    print(f"Extracted: {len(records)}; failed: {len(errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
