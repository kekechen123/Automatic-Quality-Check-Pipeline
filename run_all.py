#!/usr/bin/env python3
"""Run extraction, full browser fallback, OCR, and LLM judging as one pipeline."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = SCRIPT_DIR / "data"


def load_dotenv(path: Path, override: bool = False) -> dict[str, str]:
    """Load a small KEY=VALUE .env file without adding a dependency."""
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"Invalid .env line {line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid .env key on line {line_number}: {key!r}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


def run_stage(name: str, command: list[str], env: dict[str, str], dry_run: bool) -> None:
    printable = subprocess.list2cmdline(command)
    print(f"\n{'=' * 72}\n{name}\n{'=' * 72}\n{printable}", flush=True)
    if dry_run:
        return
    started = time.perf_counter()
    result = subprocess.run(command, cwd=SCRIPT_DIR, env=env, check=False)
    elapsed = time.perf_counter() - started
    if result.returncode != 0:
        raise RuntimeError(f"{name} failed with exit code {result.returncode} after {elapsed:.1f}s")
    print(f"{name} completed in {elapsed:.1f}s", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Annotation-platform JSONL input")
    parser.add_argument("-o", "--output", type=Path, help="Final CSV path; defaults beside run_all.py")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="All intermediate files directory")
    parser.add_argument("--env-file", type=Path, default=SCRIPT_DIR / ".env")
    parser.add_argument("--python", default=sys.executable, help="Python executable used for child scripts")
    parser.add_argument("--crawl-concurrency", type=int, default=24)
    parser.add_argument("--crawl-timeout", type=int, default=20)
    parser.add_argument("--crawl-retries", type=int, default=1)
    parser.add_argument("--ocr-download-workers", type=int, default=12)
    parser.add_argument("--ocr-download-timeout", type=int, default=30)
    parser.add_argument("--ocr-timeout", type=int, default=120)
    parser.add_argument("--judge-workers", type=int, default=3)
    parser.add_argument("--judge-timeout", type=int, default=240)
    parser.add_argument("--judge-max-tokens", type=int, default=6000)
    parser.add_argument("--judge-retries", type=int, default=2)
    parser.add_argument("--model", help="Override LLM_MODEL")
    parser.add_argument("--base-url", help="Override LLM_BASE_URL")
    parser.add_argument("--refresh-crawl", action="store_true", help="Refresh all normal URL metadata")
    parser.add_argument("--refresh-ocr", action="store_true", help="Ignore successful OCR cache (may cost money)")
    parser.add_argument("--no-resume-judge", action="store_true", help="Rejudge records already successful in JSONL")
    parser.add_argument("--dry-run", action="store_true", help="Print paths and commands without executing")
    args = parser.parse_args()

    input_path = args.input.resolve()
    if not input_path.exists():
        print(f"Input does not exist: {input_path}", file=sys.stderr)
        return 1
    data_dir = args.data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    output_path = (args.output or SCRIPT_DIR / f"{input_path.stem}_qc_result.csv").resolve()
    if output_path.parent != SCRIPT_DIR:
        print("Warning: final CSV is outside the script directory.", file=sys.stderr)

    try:
        loaded = load_dotenv(args.env_file.resolve())
    except (OSError, ValueError) as exc:
        print(f"Cannot load .env: {exc}", file=sys.stderr)
        return 1
    env = os.environ.copy()
    model = args.model or env.get("LLM_MODEL", "deepseek-v4-pro-202606")
    base_url = args.base_url or env.get("LLM_BASE_URL", "https://tokenhub.tencentmaas.com/v1")
    env["LLM_MODEL"] = model
    env["LLM_BASE_URL"] = base_url

    missing = [key for key in ("OCR_API_KEY", "llm_API_KEY") if not env.get(key, "").strip()]
    if missing and not args.dry_run:
        print(f"Missing required .env values: {', '.join(missing)}", file=sys.stderr)
        return 2

    stem = input_path.stem
    qc_md = data_dir / f"{stem}_qc_fields.md"
    url_cache = data_dir / "url_metadata_cache.json"
    ocr_md = data_dir / f"{stem}_qc_fields_ocr.md"
    image_dir = data_dir / f"{stem}_qc_fields_images"
    ocr_cache = data_dir / f"{stem}_qc_fields_ocr_cache.json"
    judge_jsonl = data_dir / f"{stem}_qc_fields_ocr_judge.jsonl"
    judge_summary = data_dir / f"{stem}_qc_fields_ocr_judge_summary.md"

    print("Pipeline paths:")
    print(f"- input: {input_path}")
    print(f"- data: {data_dir}")
    print(f"- final CSV: {output_path}")
    if loaded:
        print(f"- env loaded: {args.env_file.resolve()} ({len(loaded)} keys)")

    extract_command = [
        args.python, str(SCRIPT_DIR / "extract_qc_to_md.py"), str(input_path),
        "-o", str(qc_md), "--crawl",
        "--concurrency", str(max(1, args.crawl_concurrency)),
        "--timeout", str(max(1, args.crawl_timeout)),
        "--retries", str(max(0, args.crawl_retries)),
        "--cache", str(url_cache),
        "--browser-fallback", "--browser-fallback-limit", "0",
    ]
    extract_command.append("--refresh" if args.refresh_crawl else "--retry-failures")

    ocr_command = [
        args.python, str(SCRIPT_DIR / "ocr_and_write.py"), str(qc_md),
        "-o", str(ocr_md), "--image-dir", str(image_dir), "--cache", str(ocr_cache),
        "--endpoint", "https://api.ocr.space/parse/image",
        "--language", "auto", "--engine", "3", "--limit", "0",
        "--download-workers", str(max(1, args.ocr_download_workers)),
        "--download-timeout", str(max(1, args.ocr_download_timeout)),
        "--ocr-timeout", str(max(1, args.ocr_timeout)),
    ]
    if args.refresh_ocr:
        ocr_command.append("--refresh-ocr")

    judge_command = [
        args.python, str(SCRIPT_DIR / "llm_judge.py"), str(ocr_md),
        "-o", str(judge_jsonl), "--summary", str(judge_summary), "--csv", str(output_path),
        "--model", model, "--base-url", base_url,
        "--workers", str(max(1, args.judge_workers)),
        "--timeout", str(max(1, args.judge_timeout)),
        "--max-tokens", str(max(256, args.judge_max_tokens)),
        "--temperature", "0", "--retries", str(max(0, args.judge_retries)),
    ]
    if args.no_resume_judge:
        judge_command.append("--no-resume")

    try:
        run_stage("1/3 Extract fields + full browser fallback", extract_command, env, args.dry_run)
        run_stage("2/3 Download images + full OCR", ocr_command, env, args.dry_run)
        run_stage("3/3 LLM judge + final CSV", judge_command, env, args.dry_run)
    except (OSError, RuntimeError) as exc:
        print(f"Pipeline failed: {exc}", file=sys.stderr)
        return 3

    if args.dry_run:
        print("\nDry run completed; no subprocess was executed.")
    else:
        if not output_path.exists():
            print(f"Pipeline finished but CSV is missing: {output_path}", file=sys.stderr)
            return 4
        print(f"\nPipeline complete. Final CSV: {output_path}")
        print(f"All intermediate artifacts: {data_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())