#!/usr/bin/env python3
"""Run the QC pipeline through step 1, 2, or 3.

Step 1: extract fields and crawl sources.
Step 2: run steps 1-2, including image OCR.
Step 3: run steps 1-3, including Knot agent judging and final CSV output.
"""

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
    """Load KEY=VALUE pairs from .env without an extra dependency."""
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
        if override or not os.environ.get(key, "").strip():
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
    parser = argparse.ArgumentParser(
        description="Run QC pipeline cumulatively: --step1, --step2, or --step3 (default)."
    )
    parser.add_argument("input", type=Path, help="Annotation-platform JSONL input")
    steps = parser.add_mutually_exclusive_group()
    steps.add_argument("--step1", action="store_true", help="Only run step 1: extract and crawl")
    steps.add_argument("--step2", action="store_true", help="Run steps 1-2: extract, crawl, and OCR")
    steps.add_argument("--step3", action="store_true", help="Run steps 1-3: full pipeline (default)")
    parser.add_argument("-o", "--output", type=Path, help="Final CSV path for step 3")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="Intermediate files directory")
    parser.add_argument("--env-file", type=Path, default=SCRIPT_DIR / ".env", help="Environment configuration")
    parser.add_argument("--refresh", action="store_true", help="Refresh URL crawl and OCR caches")
    parser.add_argument("--no-resume", action="store_true", help="Rejudge successful existing Knot results")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing them")
    args = parser.parse_args()

    target_step = 1 if args.step1 else 2 if args.step2 else 3
    input_path = args.input.resolve()
    if not input_path.exists():
        print(f"Input does not exist: {input_path}", file=sys.stderr)
        return 1

    data_dir = args.data_dir.resolve()
    if not args.dry_run:
        data_dir.mkdir(parents=True, exist_ok=True)
    output_path = (args.output or SCRIPT_DIR / f"{input_path.stem}_qc_result.csv").resolve()
    env_file = args.env_file.resolve()

    try:
        loaded = load_dotenv(env_file)
    except (OSError, ValueError) as exc:
        print(f"Cannot load .env: {exc}", file=sys.stderr)
        return 1
    env = os.environ.copy()

    required: list[str] = []
    if target_step >= 2:
        required.append("OCR_API_KEY")
    if target_step >= 3:
        required.extend(("KNOT_API_TOKEN", "KNOT_API_URL", "KNOT_API_USER", "KNOT_AGENT_CLIENT_UUID"))
    missing = [key for key in required if not env.get(key, "").strip()]
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

    print(f"Target: step {target_step}")
    print(f"Input: {input_path}")
    print(f"Data: {data_dir}")
    if target_step >= 3:
        print(f"Final CSV: {output_path}")
        print(f"Knot agent: {env.get('KNOT_API_URL', '(not configured)')}")
    if loaded:
        print(f"Environment: {env_file} ({len(loaded)} keys loaded)")

    extract_command = [
        sys.executable, str(SCRIPT_DIR / "extract_qc_to_md.py"), str(input_path),
        "-o", str(qc_md), "--crawl",
        "--concurrency", "24", "--timeout", "20", "--retries", "1",
        "--cache", str(url_cache),
        "--browser-fallback", "--browser-fallback-limit", "0",
        "--refresh" if args.refresh else "--retry-failures",
    ]

    ocr_command = [
        sys.executable, str(SCRIPT_DIR / "ocr_and_write.py"), str(qc_md),
        "--env-file", str(env_file),
        "-o", str(ocr_md), "--image-dir", str(image_dir), "--cache", str(ocr_cache),
        "--endpoint", env.get("OCR_ENDPOINT", "https://www.evern.ccwu.cc/ocr"),
        "--limit", "0", "--download-workers", "12", "--download-timeout", "30",
        "--ocr-timeout", "120", "--ocr-delay", "5",
        "--ocr-retries", "8", "--ocr-retry-base-delay", "15", "--ocr-retry-max-delay", "300",
    ]
    if args.refresh:
        ocr_command.append("--refresh-ocr")

    judge_command = [
        sys.executable, str(SCRIPT_DIR / "llm_judge.py"), str(ocr_md),
        "--env-file", str(env_file),
        "-o", str(judge_jsonl), "--summary", str(judge_summary), "--csv", str(output_path),
        "--workers", "3", "--timeout", "240", "--retries", "2",
    ]
    if args.no_resume:
        judge_command.append("--no-resume")

    stages = [
        ("Step 1/3 - Extract fields and crawl sources", extract_command),
        ("Step 2/3 - Download images and OCR", ocr_command),
        ("Step 3/3 - Judge with Knot agent and write CSV", judge_command),
    ]

    try:
        for name, command in stages[:target_step]:
            run_stage(name, command, env, args.dry_run)
    except (OSError, RuntimeError) as exc:
        print(f"Pipeline failed: {exc}", file=sys.stderr)
        return 3

    if args.dry_run:
        print(f"\nDry run completed through step {target_step}; nothing was executed.")
        return 0

    expected_output = qc_md if target_step == 1 else ocr_md if target_step == 2 else output_path
    if not expected_output.exists():
        print(f"Pipeline finished but expected output is missing: {expected_output}", file=sys.stderr)
        return 4

    print(f"\nCompleted through step {target_step}.")
    print(f"Output: {expected_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
