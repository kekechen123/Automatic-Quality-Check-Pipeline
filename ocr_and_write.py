#!/usr/bin/env python3
"""Download Markdown images, OCR them, and write a link-free Markdown copy.

The default OCR endpoint is OCR.Space. Set OCR_API_KEY in the environment; the
key is never written to output, logs, or cache files.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<url>(?:[^()\s]|\([^()]*\))+)(?:\s+[\"'][^\"']*[\"'])?\)", re.IGNORECASE)
DEFAULT_ENDPOINT = "https://api.ocr.space/parse/image"


def clean_image_url(value: str) -> str:
    value = value.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    return value


def load_cache(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(path: Path, cache: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(cache, ensure_ascii=False, indent=2)
    temp.write_text(payload, encoding="utf-8")
    for attempt in range(5):
        try:
            temp.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                # Some Windows antivirus/indexers briefly lock JSON files.
                path.write_text(payload, encoding="utf-8")
                temp.unlink(missing_ok=True)
                return
            time.sleep(0.2 * (attempt + 1))


def image_path_for(url: str, directory: Path, index: int) -> Path:
    suffix = Path(urlsplit(url).path).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", suffix):
        suffix = ".img"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return directory / f"{index:04d}_{digest}{suffix}"


def download_image(url: str, target: Path, timeout: float, overwrite: bool) -> None:
    import requests
    if target.exists() and target.stat().st_size > 0 and not overwrite:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "Mozilla/5.0 QC-Image-OCR/1.0"}
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(url, headers=headers, timeout=timeout, stream=True)
        with response:
            response.raise_for_status()
            temp = target.with_suffix(target.suffix + ".part")
            with temp.open("wb") as stream:
                for chunk in response.iter_content(1024 * 256):
                    if chunk:
                        stream.write(chunk)
            if temp.stat().st_size == 0:
                temp.unlink(missing_ok=True)
                raise RuntimeError("downloaded an empty file")
            temp.replace(target)


def parse_ocr_response(data: Any) -> str:
    if not isinstance(data, dict):
        raise RuntimeError("OCR returned a non-object response")
    if data.get("IsErroredOnProcessing"):
        message = data.get("ErrorMessage") or data.get("ErrorDetails") or "OCR processing failed"
        if isinstance(message, list):
            message = "; ".join(str(item) for item in message)
        raise RuntimeError(str(message))
    results = data.get("ParsedResults") or []
    texts = [str(item.get("ParsedText") or "").strip() for item in results if isinstance(item, dict)]
    text = "\n\n".join(item for item in texts if item).strip()
    return text or "\uff08\u672a\u8bc6\u522b\u5230\u6587\u5b57\uff09"


def call_ocr_space(path: Path, api_key: str, endpoint: str, language: str, timeout: float, engine: int) -> str:
    # OCR.Space Engine 3 supports language=auto and preserves mixed Chinese/English
    # text in a single pass.  This avoids paying for separate eng/chs requests.
    if language == "auto" and engine != 3:
        raise ValueError("language=auto requires OCR engine 3")
    import requests
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as image:
        session = requests.Session()
        session.trust_env = False
        response = session.post(
            endpoint,
            headers={"apikey": api_key},
            files={"file": (path.name, image, mime)},
            data={
                "language": language,
                "isOverlayRequired": "false",
                "detectOrientation": "true",
                "scale": "true",
                "OCREngine": str(engine),
            },
            timeout=timeout,
        )
    response.raise_for_status()
    return parse_ocr_response(response.json())


def replacement(text: str) -> str:
    text = text.strip() or "\uff08\u672a\u8bc6\u522b\u5230\u6587\u5b57\uff09"
    return f"\uff08\u6b64\u5904\u662f\u56fe\u7247ocr\u7ed3\u679c\uff1a\n{text}\n\uff09"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Markdown file containing image links")
    parser.add_argument("-o", "--output", type=Path, help="Output Markdown; defaults to *_ocr.md")
    parser.add_argument("--image-dir", type=Path, help="Directory used to keep downloaded images")
    parser.add_argument("--cache", type=Path, help="OCR result cache JSON")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="OCR API endpoint")
    parser.add_argument(
        "--language", choices=("auto", "eng", "chs"), default="auto",
        help="OCR language: auto detects mixed Chinese/English (default); eng/chs force one language",
    )
    parser.add_argument("--engine", type=int, choices=(1, 2, 3), default=3, help="OCR.Space engine; auto language requires engine 3")
    parser.add_argument("--limit", type=int, default=0, help="OCR only the first N unique images; 0 means all")
    parser.add_argument("--download-timeout", type=float, default=30, help="Image download timeout in seconds")
    parser.add_argument("--download-workers", type=int, default=12, help="Concurrent image downloads")
    parser.add_argument("--ocr-timeout", type=float, default=120, help="OCR request timeout in seconds")
    parser.add_argument("--overwrite-images", action="store_true", help="Redownload existing image files")
    parser.add_argument("--refresh-ocr", action="store_true", help="Ignore successful cached OCR results")
    parser.add_argument("--keep-unprocessed-links", action="store_true", help="With --limit, retain image links not OCRed")
    args = parser.parse_args()
    if args.language == "auto" and args.engine != 3:
        parser.error("--language auto requires --engine 3")

    input_path = args.input.resolve()
    output_path = (args.output or input_path.with_name(f"{input_path.stem}_ocr.md")).resolve()
    image_dir = (args.image_dir or input_path.with_name(f"{input_path.stem}_images")).resolve()
    cache_path = (args.cache or input_path.with_name(f"{input_path.stem}_ocr_cache.json")).resolve()
    api_key = os.environ.get("OCR_API_KEY", "").strip()
    if not api_key:
        print("Missing environment variable OCR_API_KEY", file=sys.stderr)
        return 2

    try:
        source = input_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Cannot read input: {exc}", file=sys.stderr)
        return 1

    matches = list(IMAGE_RE.finditer(source))
    urls = list(dict.fromkeys(clean_image_url(match.group("url")) for match in matches))
    selected = urls[: args.limit] if args.limit > 0 else urls
    cache = load_cache(cache_path)
    image_dir.mkdir(parents=True, exist_ok=True)
    print(f"Found {len(matches)} image occurrences, {len(urls)} unique; selected {len(selected)} for OCR.")

    # Always download every unique image.  --limit only caps paid OCR calls.
    paths = {url: image_path_for(url, image_dir, index) for index, url in enumerate(urls, 1)}
    download_failures: dict[str, str] = {}

    def fetch(item: tuple[int, str]) -> tuple[int, str, str]:
        index, url = item
        target = paths[url]
        try:
            download_image(url, target, args.download_timeout, args.overwrite_images)
            return index, url, ""
        except Exception as exc:
            return index, url, f"{type(exc).__name__}: {exc}"

    workers = min(max(1, args.download_workers), max(1, len(urls)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, item) for item in enumerate(urls, 1)]
        completed = 0
        for future in concurrent.futures.as_completed(futures):
            index, url, error = future.result()
            completed += 1
            if error:
                download_failures[url] = error
                print(f"[download {completed}/{len(urls)}] #{index} failed: {error}", file=sys.stderr)
            else:
                print(f"[download {completed}/{len(urls)}] #{index} ok")

    results: dict[str, str] = {}
    failures = 0
    for index, url in enumerate(selected, 1):
        target = paths[url]
        cached = cache.get(url) or {}
        cache_matches_mode = (
            cached.get("language") == args.language
            and cached.get("engine") == args.engine
            and cached.get("endpoint", DEFAULT_ENDPOINT) == args.endpoint
        )
        if cached.get("success") and cached.get("text") and cache_matches_mode and not args.refresh_ocr:
            results[url] = str(cached["text"])
            print(f"[OCR {index}/{len(selected)}] cache: {target.name}")
            continue
        if url in download_failures:
            failures += 1
            message = download_failures[url]
            results[url] = f"ocr\u5931\u8d25\uff1a\u56fe\u7247\u4e0b\u8f7d\u5931\u8d25\uff1a{message}"
            cache[url] = {
                "success": False, "text": "", "image_path": str(target),
                "language": args.language, "engine": args.engine, "endpoint": args.endpoint,
                "ocr_at": time.strftime("%Y-%m-%d %H:%M:%S"), "error": message,
            }
            save_cache(cache_path, cache)
            continue
        try:
            print(f"[OCR {index}/{len(selected)}] {target.name}")
            text = call_ocr_space(target, api_key, args.endpoint, args.language, args.ocr_timeout, args.engine)
            results[url] = text
            cache[url] = {
                "success": True, "text": text, "image_path": str(target),
                "language": args.language, "engine": args.engine, "endpoint": args.endpoint,
                "ocr_at": time.strftime("%Y-%m-%d %H:%M:%S"), "error": "",
            }
        except Exception as exc:
            failures += 1
            message = f"{type(exc).__name__}: {exc}"
            results[url] = f"ocr\u5931\u8d25\uff1a{message}"
            cache[url] = {
                "success": False, "text": "", "image_path": str(target),
                "language": args.language, "engine": args.engine, "endpoint": args.endpoint,
                "ocr_at": time.strftime("%Y-%m-%d %H:%M:%S"), "error": message,
            }
            print(f"[OCR {index}/{len(selected)}] failed: {message}", file=sys.stderr)
        save_cache(cache_path, cache)

    def replace_match(match: re.Match[str]) -> str:
        url = clean_image_url(match.group("url"))
        if url in results:
            return replacement(results[url])
        if args.keep_unprocessed_links:
            return match.group(0)
        return replacement("\uff08\u672a\u6267\u884cocr\uff1a\u672c\u6b21\u6d4b\u8bd5\u6570\u91cf\u53d7 --limit \u9650\u5236\uff09")

    rendered = IMAGE_RE.sub(replace_match, source)
    output_path.write_text(rendered, encoding="utf-8")
    print(f"Output: {output_path}")
    print(f"Images: {image_dir}")
    print(f"Cache: {cache_path}")
    print(f"Downloads: {len(urls) - len(download_failures)}/{len(urls)}; OCR success: {len(selected) - failures}; failed: {failures}; unprocessed: {len(urls) - len(selected)}")
    return 0 if failures == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
